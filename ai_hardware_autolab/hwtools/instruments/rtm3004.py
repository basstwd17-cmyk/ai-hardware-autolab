"""Rohde & Schwarz RTM3004 -- fixed part only.

What belongs here: identity, state that is true regardless of what is being
measured, mechanisms for getting data out, and putting the instrument back in
a safe state.

What deliberately does NOT belong here: channel scale, timebase, trigger level,
which measurement to enable, how many samples are enough, what counts as a
pass. Those change with every experiment and are written as a recipe on top of
this class. Freezing them here is what made the previous driver's 200-line
configure() unusable for anything but one delay sweep.

SCPI syntax below was checked against the RTM3000 user manual
(tmp/pdfs/RTM3000_UserManual_en_11.txt), not recalled.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..scpi import ScpiError, ScpiTransport

# CHANnel<m>:COUPling selects the input termination on this instrument. There
# is no CHANnel<m>:IMPedance command. Getting this backwards loads the DUT:
# a 4 mA LVCMOS33 output into 50 ohm collapses well below its logic high.
COUPLING_TERMINATION = {
    "DC": "50_ohm",
    "DCL": "1_Mohm",
    "DCLIMIT": "1_Mohm",
    "ACL": "1_Mohm_ac",
    "ACLIMIT": "1_Mohm_ac",
    "GND": "ground",
}
HIGH_IMPEDANCE = {"1_Mohm", "1_Mohm_ac"}

# STATus:OPERation:CONDition bit 3 -- set while the instrument is armed and
# waiting for a trigger.
WAITING_FOR_TRIGGER_BIT = 1 << 3


@dataclass(frozen=True)
class ScopeIdentity:
    manufacturer: str
    model: str
    serial: str
    firmware: str
    options: tuple[str, ...]
    raw: str

    def as_versions(self) -> dict[str, str]:
        return {
            "scope": f"{self.manufacturer} {self.model}",
            "scope_serial": self.serial,
            "scope_firmware": self.firmware,
            "scope_options": ",".join(self.options),
        }


@dataclass(frozen=True)
class AcquisitionState:
    state: str
    waiting_for_trigger: bool
    condition_register: int

    @property
    def ready(self) -> bool:
        """True when the instrument is running or armed.

        ACQ:STAT? alone is not enough. While a Nx Single acquisition is armed
        and waiting, this instrument reports STOP. Treating that as "not
        running" aborts a perfectly good measurement, so the condition register
        has to be read alongside it.
        """
        return self.state.startswith("RUN") or self.waiting_for_trigger

    @property
    def complete(self) -> bool:
        return self.state.startswith("COMP")


@dataclass(frozen=True)
class MeasurementResult:
    index: int
    actual: float | None
    average: float | None
    stddev: float | None
    negative_peak: float | None
    positive_peak: float | None
    waveform_count: int
    values: list[float]

    def as_measurements(self, prefix: str) -> dict[str, Any]:
        return {
            f"{prefix}_actual": self.actual,
            f"{prefix}_average": self.average,
            f"{prefix}_stddev": self.stddev,
            f"{prefix}_min": self.negative_peak,
            f"{prefix}_max": self.positive_peak,
            f"{prefix}_peak_to_peak": (
                None
                if self.positive_peak is None or self.negative_peak is None
                else self.positive_peak - self.negative_peak
            ),
            f"{prefix}_waveform_count": self.waveform_count,
            f"{prefix}_sample_count": len(self.values),
        }


@dataclass(frozen=True)
class FrontEnd:
    """Everything needed to interpret a voltage from one channel.

    Probe attenuation scales every reading, and the instrument changes it by
    itself when it detects a probe -- CH3 went from 1:1 to 10:1 the moment a
    passive probe was plugged in. A voltage recorded without it cannot be
    compared against one taken on another day.
    """

    channel: int
    coupling: str
    termination: str
    probe_attenuation: float | None
    scale_v_per_div: float | None
    offset_v: float | None
    bandwidth_limit: str

    def as_measurements(self) -> dict[str, Any]:
        return {
            f"CH{self.channel}_coupling": self.coupling,
            f"CH{self.channel}_termination": self.termination,
            f"CH{self.channel}_probe_attenuation": self.probe_attenuation,
            f"CH{self.channel}_scale_v_per_div": self.scale_v_per_div,
            f"CH{self.channel}_offset_v": self.offset_v,
            f"CH{self.channel}_bandwidth_limit": self.bandwidth_limit,
        }


@dataclass(frozen=True)
class Waveform:
    channel: int
    x_start_s: float
    x_stop_s: float
    record_length: int
    values_per_sample: int
    values_v: list[float]

    @property
    def x_increment_s(self) -> float:
        span = self.x_stop_s - self.x_start_s
        return span / (self.record_length - 1) if self.record_length > 1 else 0.0

    def times_s(self) -> list[float]:
        step = self.x_increment_s
        return [self.x_start_s + i * step for i in range(len(self.values_v))]


class RTM3004:
    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout_s: float = 5.0,
        mock: bool = False,
        mock_responses: dict[str, str] | None = None,
    ) -> None:
        self.scpi = ScpiTransport(
            host, port, timeout_s=timeout_s, mock=mock, mock_responses=mock_responses
        )

    # -- identity ------------------------------------------------------------

    def identify(self) -> ScopeIdentity:
        raw = self.scpi.query("*IDN?")
        parts = [p.strip() for p in raw.split(",")]
        manufacturer, model, serial, firmware = (parts + ["", "", "", ""])[:4]
        options = tuple(
            o.strip() for o in self.scpi.query("*OPT?").split(",") if o.strip()
        )
        return ScopeIdentity(manufacturer, model, serial, firmware, options, raw)

    def require_option(self, code: str) -> None:
        """Refuse when a needed option is missing, instead of measuring wrong.

        A missing option does not make the relevant commands fail loudly; some
        are simply ignored, which yields a plausible but meaningless number.
        """
        options = self.identify().options
        if code.upper() not in {o.upper() for o in options}:
            raise ScpiError(
                f"RTM3004 option {code} is required but *OPT? reports: "
                f"{','.join(options) or '<none>'}"
            )

    def check_errors(self, context: str = "") -> None:
        self.scpi.check_errors(context)

    # -- state ---------------------------------------------------------------

    def acquisition_state(self) -> AcquisitionState:
        state = self.scpi.query("ACQ:STAT?").upper()
        condition = int(float(self.scpi.query("STAT:OPER:COND?")))
        return AcquisitionState(
            state=state,
            waiting_for_trigger=bool(condition & WAITING_FOR_TRIGGER_BIT),
            condition_register=condition,
        )

    def waveform_count(self, measurement: int = 1) -> int:
        raw = float(self.scpi.query(f"MEAS{measurement}:RES:WFMC?"))
        # 9.91e37 is the instrument's "no value yet" marker, not a count.
        return 0 if raw > 1e36 else int(raw)

    def channel_termination(self, channel: int) -> str:
        coupling = self.scpi.query(f"CHAN{channel}:COUP?").strip().upper()
        return COUPLING_TERMINATION.get(coupling, f"unknown({coupling})")

    def front_end(self, channel: int) -> FrontEnd:
        """Read the settings that determine what a voltage on this channel means."""
        coupling = self.scpi.query(f"CHAN{channel}:COUP?").strip().upper()
        return FrontEnd(
            channel=channel,
            coupling=coupling,
            termination=COUPLING_TERMINATION.get(coupling, f"unknown({coupling})"),
            probe_attenuation=self._query_optional_float(f"PROB{channel}:SET:ATT:MAN?"),
            scale_v_per_div=self._query_optional_float(f"CHAN{channel}:SCAL?"),
            offset_v=self._query_optional_float(f"CHAN{channel}:OFFS?"),
            bandwidth_limit=self.scpi.query(f"CHAN{channel}:BAND?").strip().upper(),
        )

    def assert_high_impedance(self, *channels: int) -> dict[int, str]:
        """Guard a measurement against a 50 ohm input loading the DUT.

        Gate 1 probes a 4 mA LVCMOS33 output. On a 50 ohm input that is roughly
        66 mA at 3.3 V, so the level reads low and the pin is stressed. The
        failure looks like a bad design rather than a bad termination, which is
        why this is checked rather than assumed.
        """
        terminations: dict[int, str] = {}
        offenders: list[str] = []
        for channel in channels:
            termination = self.channel_termination(channel)
            terminations[channel] = termination
            if termination not in HIGH_IMPEDANCE:
                offenders.append(f"CH{channel}={termination}")
        if offenders:
            raise ScpiError(
                "Refusing to measure into a low-impedance input: "
                + ", ".join(offenders)
                + ". Set CHANnel:COUPling to DCLimit for 1 Mohm, or probe on a "
                "channel that already is."
            )
        return terminations

    # -- data retrieval ------------------------------------------------------

    def _query_optional_float(self, command: str) -> float | None:
        try:
            value = float(self.scpi.query(command))
        except (ScpiError, ValueError):
            return None
        return None if abs(value) > 1e36 else value

    def read_measurement(self, index: int = 1) -> MeasurementResult:
        """Pull every statistic the instrument holds for one measurement place.

        Which measurement type sits in that place is the recipe's decision;
        retrieving it is not.
        """
        values: list[float] = []
        try:
            raw = self.scpi.query(f"MEAS{index}:STAT:VAL:ALL?")
            values = [
                float(v) for v in raw.split(",") if v.strip() and abs(float(v)) < 1e36
            ]
        except (ScpiError, ValueError):
            values = []
        return MeasurementResult(
            index=index,
            actual=self._query_optional_float(f"MEAS{index}:RES:ACT?"),
            average=self._query_optional_float(f"MEAS{index}:RES:AVG?"),
            stddev=self._query_optional_float(f"MEAS{index}:RES:STDD?"),
            negative_peak=self._query_optional_float(f"MEAS{index}:RES:NPE?"),
            positive_peak=self._query_optional_float(f"MEAS{index}:RES:PPE?"),
            waveform_count=self.waveform_count(index),
            values=values,
        )

    def read_waveform(self, channel: int, binary: bool = True) -> Waveform:
        """Read one channel's samples plus the header needed to place them in time.

        The header must be read for every capture: without XStart/XStop the
        sample array is just numbers with no time axis, and a later timebase
        change would silently reinterpret an old capture.
        """
        header = self.scpi.query(f"CHAN{channel}:DATA:HEAD?")
        fields = [f.strip() for f in header.split(",")]
        x_start, x_stop = float(fields[0]), float(fields[1])
        record_length, values_per_sample = int(float(fields[2])), int(float(fields[3]))

        if binary:
            self.scpi.write("FORM REAL,32")
            self.scpi.write("FORM:BORD LSBF")
            payload = self.scpi.query_binary_block(f"CHAN{channel}:DATA?")
            count = len(payload) // 4
            values = list(struct.unpack(f"<{count}f", payload[: count * 4]))
        else:
            self.scpi.write("FORM ASC")
            raw = self.scpi.query(f"CHAN{channel}:DATA?")
            values = [float(v) for v in raw.split(",") if v.strip()]

        waveform = Waveform(
            channel=channel,
            x_start_s=x_start,
            x_stop_s=x_stop,
            record_length=record_length,
            values_per_sample=values_per_sample,
            values_v=values,
        )
        # The host persists acquisition evidence before recipe-side analysis.
        # A copied instrument still uses its own (possibly bounded) SCPI object.
        sink = getattr(self, "waveform_sink", None)
        if sink is not None:
            sink(waveform)
        return waveform

    def save_screenshot(self, path: Path) -> Path:
        """Transfer the live screen.

        Always transferred from the display, never copied from the instrument's
        internal storage, so the image cannot be a stale file from an earlier run.
        """
        self.scpi.write("HCOP:LANG PNG")
        payload = self.scpi.query_binary_block("HCOP:DATA?")
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ScpiError(
                f"HCOP:DATA? did not return a PNG ({len(payload)} bytes). "
                "A truncated transfer here silently produces an unopenable "
                "artifact that only shows up when someone reviews the run."
            )
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    # -- safe state ----------------------------------------------------------

    def configure_record_length(self, points: int) -> int:
        """Select acquisition size before capture; record the instrument's value.

        RTM3000 manual, ACQuire:POINts[:VALue]: setting a value disables
        automatic record length and the instrument may choose the nearest size.
        This is not host-side decimation and never discards a captured trace.
        """
        if type(points) is not int or points < 2:
            raise ValueError("record_length must be an integer of at least 2 samples")
        self.scpi.write(f"ACQ:POIN {points}")
        self.check_errors("record length")
        raw = float(self.scpi.query("ACQ:POIN?"))
        if not math.isfinite(raw) or raw < 2 or raw != int(raw):
            raise ScpiError(f"Invalid record length readback: {raw}")
        return int(raw)

    def arm_nsingle(self, count: int) -> None:
        """Arm exactly `count` acquisitions and start them.

        Requires the K15 history option. Without it `ACQuire:NSINgle:COUNt` is
        accepted and ignored, the acquisition free-runs, and the statistics
        silently cover a different N than was asked for -- which is worse than
        an error, because the wrong N still gets quoted in the result.
        """
        if count < 1:
            raise ValueError(f"count must be at least 1, got {count}")
        self.require_option("K15")
        self.scpi.write(f"ACQ:NSIN:COUN {count}")
        self.scpi.write("SING")

    def arm_single(self) -> None:
        """One observation, without inheriting a previous Nx Single count."""
        if "K15" in self.identify().options:
            self.scpi.write("ACQ:NSIN:COUN 1")
            self.check_errors("single acquisition count")
        self.scpi.write("SING")

    def wait_until_complete(
        self, timeout_s: float, poll_s: float = 0.05
    ) -> AcquisitionState:
        """Block until the armed acquisition reports COMPlete.

        Readiness is judged through AcquisitionState, never ACQ:STAT? alone:
        this instrument reports STOP while armed and waiting for a trigger.
        A stop is only treated as a failure once the acquisition has been seen
        active, so the gap between SINGle and the instrument actually starting
        is not mistaken for an abort.
        """
        deadline = time.monotonic() + timeout_s
        state = self.acquisition_state()
        seen_active = False
        while time.monotonic() < deadline:
            state = self.acquisition_state()
            if state.complete:
                return state
            if state.ready:
                seen_active = True
            elif seen_active:
                raise ScpiError(
                    f"Acquisition stopped before completing (state={state.state}, "
                    f"condition=0x{state.condition_register:04X})"
                )
            time.sleep(poll_s)
        raise ScpiError(
            f"Acquisition did not complete within {timeout_s:g}s "
            f"(last state={state.state}, "
            f"condition=0x{state.condition_register:04X}). "
            f"Check that the trigger source carries a signal and that the level "
            f"is inside it."
        )

    def stop(self) -> None:
        self.scpi.write("STOP")
        self.scpi.query("*OPC?")

    def emergency_stop(self) -> None:
        """Best effort return to a harmless state. Never raises.

        Called from failure paths, so it must not mask the original error with
        one of its own.
        """
        for command in ("WGEN:OUTP OFF", "STOP"):
            try:
                self.scpi.write(command)
            except Exception:  # noqa: BLE001 - deliberate: never mask the cause
                pass

    def close(self) -> None:
        self.scpi.close()

    def __enter__(self) -> "RTM3004":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            self.emergency_stop()
        self.close()

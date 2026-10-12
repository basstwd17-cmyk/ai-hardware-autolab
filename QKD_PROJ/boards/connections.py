"""Identify devices in the installed setup without programming or measuring.

This is an observation, not an authorization or a wiring detector. The current
drivers support one BBB, scope and JTAG chain per profile. Unknown/ambiguous
devices remain visible and unverified until a suitable driver is available.
"""
from __future__ import annotations

import copy
import threading
from collections import Counter
from pathlib import Path

from QKD_PROJ.hwtools.result import utc_now
from .hardware import HardwareProfile


class HardwareConnections:
    def __init__(self, profile: HardwareProfile, runs_dir: Path, *, probe=None):
        self.profile, self.runs_dir = profile, Path(runs_dir)
        self._probe = probe or self._identify
        # Shared with actual device execution: a scan cannot race programming.
        self.device_lock = threading.RLock()
        self._changed = threading.Condition()
        self._snapshot = self._empty()
        self._active = None

    def _empty(self):
        return {
            "setup": self.profile.public_description(), "status": "not_checked",
            "started_at": None, "checked_at": None,
            "requested_devices": [],
            "devices": [{**row, "status": "not_checked", "checked_at": None}
                        for row in self.profile.public_description()["devices"]],
            "wiring_source": "saved configuration and user declarations; not detected",
        }

    def snapshot(self):
        with self._changed:
            return copy.deepcopy(self._snapshot)

    def selected_devices(self, devices):
        """No omitted/empty selection can turn into an implicit full scan."""
        if not isinstance(devices, list) or not devices or any(not isinstance(d, str) for d in devices):
            raise ValueError("devices must be a nonempty list of installed device kinds")
        if set(devices) - self.profile.device_selectors:
            raise ValueError("requested device is not in the installed setup")
        return tuple(sorted(set(devices)))

    def _begin(self, selected, cancelled):
        # Called with the condition held. Same-target requests may share a live
        # check; a different request must never receive its result by mistake.
        if self._active is not None:
            if self._active["devices"] != selected:
                raise ValueError("another device check is running; retry after it finishes")
            return self._active
        operation = {"devices": selected, "result": None}
        self._active = operation
        self._snapshot.update(status="checking", started_at=utc_now(), checked_at=None,
                              requested_devices=list(selected))
        self._snapshot.pop("error", None)
        for index, row in enumerate(self._empty()["devices"]):
            if row.get("id", row["kind"]) in selected:
                self._snapshot["devices"][index] = row
        worker = threading.Thread(target=self._run, args=(operation, cancelled), daemon=True,
                                  name="hardware-connections")
        try:
            worker.start()
        except Exception:
            self._snapshot.update(status="error", error="Could not start connection check")
            self._finish(operation)
        return operation

    def _finish(self, operation):
        self._snapshot["checked_at"] = utc_now()
        result = copy.deepcopy(self._snapshot)
        result["devices"] = [row for row in result["devices"] if row.get("id", row["kind"]) in operation["devices"]]
        operation["result"] = result
        self._active = None
        self._changed.notify_all()

    def start(self, devices, *, cancelled=None):
        """Refresh explicitly selected devices; status reads do not probe."""
        selected = self.selected_devices(devices)
        with self._changed:
            self._begin(selected, cancelled)
            return copy.deepcopy(self._snapshot)

    def check(self, devices, *, cancelled=None):
        selected = self.selected_devices(devices)
        with self._changed:
            while self._active is not None and self._active["devices"] != selected:
                if cancelled is not None and cancelled():
                    raise RuntimeError("job stopped during connection check")
                self._changed.wait(0.2)
            operation = self._begin(selected, cancelled)
            while operation["result"] is None:
                if cancelled is not None and cancelled():
                    raise RuntimeError("job stopped during connection check")
                self._changed.wait(0.2)
            return copy.deepcopy(operation["result"])

    def _run(self, operation, cancelled):
        try:
            with self.device_lock:
                fresh = HardwareProfile.load(self.profile.project_root, self.profile.board_id,
                                             self.profile.revision)
                if fresh.sha256 != self.profile.sha256:
                    raise ValueError("hardware profile changed; restart with the updated setup")
                rows = self.snapshot()["devices"]
                counts = Counter(row.get("kind") for row in rows)
                for index, row in enumerate(rows):
                    selector = row.get("id", row["kind"])
                    if selector not in operation["devices"]:
                        continue
                    if cancelled is not None and cancelled():
                        with self._changed:
                            self._snapshot["status"] = "cancelled"
                        break
                    kind = row.get("kind")
                    update = {"status": "unsupported", "checked_at": utc_now(),
                              "detail": "No unambiguous identity driver for this device"}
                    if self.profile.data.get("schema_version") == 2 and kind in {"usb", "serial"}:
                        from QKD_PROJ.hwtools.local_board import observe_local
                        device = next(d for d in self.profile.data["devices"] if d["id"] == selector)
                        try:
                            payload = observe_local(self.profile, device, self.runs_dir)
                            present = payload["result"]["measurements"]["connection_present"]
                            update = {"status": "present" if present else "unavailable", "checked_at": utc_now(),
                                      "run": payload["run"], "observed": payload["result"]["measurements"]["observed"],
                                      "detail": "OS connection present; board model, firmware and wiring are not verified" if present
                                                else payload["result"]["error"]}
                        except Exception as exc:
                            update = {"status": "error", "checked_at": utc_now(), "detail": f"Enumeration failed ({type(exc).__name__})"}
                    elif kind in {"bbb", "scope", "cpld"} and counts[kind] == 1:
                        try:
                            update = self._observation(self._probe(kind), kind)
                        except Exception as exc:
                            # Arbitrary exception text may include connection secrets.
                            update = {"status": "error", "checked_at": utc_now(),
                                      "detail": f"Identity check failed ({type(exc).__name__})"}
                    with self._changed:
                        self._snapshot["devices"][index].update(update)
        except Exception as exc:
            with self._changed:
                self._snapshot.update(status="error", error=f"Setup check failed ({type(exc).__name__}); check the installed profile")
        finally:
            with self._changed:
                if self._snapshot["status"] == "checking":
                    self._snapshot["status"] = "checked"
                self._finish(operation)

    @staticmethod
    def _observation(payload, kind):
        result = payload.get("result") or {}
        measurements = result.get("measurements") or {}
        verified = measurements.get("chain_verified" if kind == "cpld" else "identity_verified") is True
        connected = result.get("ok") is True and verified and payload.get("run_verdict") == "PASS"
        # Keep expected model/endpoint separate from the actual returned identity.
        observed = {key: measurements[key] for key in (
            "model", "target", "identification_response", "idcodes", "idcode_source", "device_count", "returncode",
        ) if key in measurements}
        return {
            "status": "connected" if connected else "unavailable", "checked_at": utc_now(),
            "observed": observed, "run": payload.get("run"),
            "failure_stage": measurements.get("failure_stage"),
            "detail": ("Device identity verified; wiring and functionality are not verified" if connected
                       else result.get("error") or "Identity not verified; inspect the run for connection, credentials, driver or model errors"),
        }

    def _identify(self, kind):
        from QKD_PROJ.hwtools.record import RunRecord
        from QKD_PROJ.hwtools.bbb import identify_bbb
        from QKD_PROJ.hwtools.scope import identify_scope
        from QKD_PROJ.hwtools.jtag import scan_jtag

        gate = self.profile.compatibility_gate()
        record = RunRecord(f"connection_{kind}", gate, runs_dir=self.runs_dir)
        if kind == "bbb":
            result = identify_bbb(gate, record, timeout_s=10.0)
        elif kind == "scope":
            result = identify_scope(gate, record)
        else:
            xcf = self.profile.data.get("jtag", {}).get("scan_xcf")
            path = self.profile._inside(self.profile.project_root, xcf, "jtag.scan_xcf")
            result = scan_jtag(gate, record, xcf_path=path, timeout_s=15.0)
        return {"run": record.dir.name, "run_verdict": record.finish(result.verdict),
                "result": result.to_dict()}

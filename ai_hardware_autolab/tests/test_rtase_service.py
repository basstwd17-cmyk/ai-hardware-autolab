"""Offline safety checks for the RTASE PRU boot service."""

from pathlib import Path


BBB = Path(__file__).resolve().parents[2] / "BeagleBone-Black"
SCRIPT = (BBB / "scripts" / "rtase-pru-service.sh").read_text(encoding="utf-8")
UNIT = (BBB / "systemd" / "rtase-pru.service").read_text(encoding="utf-8")
PROVISION = (BBB / "host" / "provision_rtase.py").read_text(encoding="utf-8")
FIRMWARE = (BBB / "firmware" / "main.c").read_text(encoding="utf-8")


def test_service_discovers_pru0_by_name_not_remoteproc_number() -> None:
    assert "4a334000.pru" in SCRIPT
    assert "*pru0*" in SCRIPT
    assert "/sys/class/remoteproc/remoteproc1" not in SCRIPT


def test_service_loads_rtase_firmware_and_waits_for_rpmsg() -> None:
    assert "am335x-pru0-rtase.out" in SCRIPT
    assert "[ ! -e /dev/rpmsg0 ]" in SCRIPT
    assert "test -e /dev/rpmsg0" in SCRIPT


def test_service_is_enabled_for_boot_and_has_bounded_startup() -> None:
    assert "WantedBy=multi-user.target" in UNIT
    assert "TimeoutStartSec=125" in UNIT
    assert "systemctl enable rtase-pru.service" in PROVISION


def test_service_idles_until_host_sends_a_command() -> None:
    assert "rtase.py" not in SCRIPT
    assert "demo" not in SCRIPT


def test_firmware_discards_stale_sequence_on_every_restart() -> None:
    main_start = FIRMWARE.index("void main(void)")
    first_command = FIRMWARE.index("while (1)", main_start)
    assert "rtase_symbol_count = 0;" in FIRMWARE[main_start:first_command]

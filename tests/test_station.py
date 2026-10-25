import json

import pytest

from bench.cli import _default_config
from bench.report import write_reports
from bench.sim import Simulator
from bench.station import EndOfLineTest, load_config

# Which step must catch each injected fault.
FAULT_CAUGHT_BY = {
    "short": "Power on",
    "rail_1v05": "Supply rails",
    "boot_hang": "Boot",
    "usb_port": "USB ports",
    "smart": "Storage",
    "no_link": "Ethernet",
    "overtemp": "Temperature",
}


def run_against(fault: str):
    config = load_config(_default_config())
    config["boot"]["timeout_s"] = 2  # the device runs 5x faster below
    with Simulator(fault=fault, time_scale=0.2,
                   ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0)) as resources:
        config["resources"] = resources
        return EndOfLineTest(config).run()


def statuses(run):
    return {step.name: step.status for step in run.steps}


def test_healthy_device_passes():
    run = run_against("none")
    assert run.status == "PASS", statuses(run)
    assert run.serial_number == "EPC-24-00137"
    assert "login:" in run.console_log


@pytest.mark.parametrize("fault,step", FAULT_CAUGHT_BY.items())
def test_fault_is_caught_by_the_right_step(fault, step):
    run = run_against(fault)
    assert run.status == "FAIL"
    result = statuses(run)
    assert result[step] == "FAIL", result
    failed = [name for name, status in result.items() if status in ("FAIL", "ERROR")]
    assert failed == [step], f"only {step} should fail, got {failed}"


def test_short_circuit_removes_power_and_skips_the_rest():
    run = run_against("short")
    result = statuses(run)
    after_power_on = list(result)[2:]
    assert all(result[name] == "SKIP" for name in after_power_on), result


def test_reports_are_written(tmp_path):
    run = run_against("none")
    folder = write_reports(run, tmp_path)
    data = json.loads((folder / "result.json").read_text())
    assert data["status"] == "PASS"
    assert (folder / "measurements.csv").read_text().count("\n") > 15
    assert "EPC-24-00137" in (folder / "report.html").read_text()

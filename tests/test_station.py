import json

import pytest

from bench.cli import _default_config, main
from bench.report import write_reports
from bench.sim import Simulator
from bench.station import EndOfLineTest, load_config

# Which step must catch each injected fault.
FAULT_CAUGHT_BY = {
    "short": "Power on",
    "rail_1v05": "Supply rails",
    "usb_port": "USB ports",
    "smart": "Storage",
    "no_link": "Ethernet",
    "overtemp": "Temperature",
    # Only the comparison with the golden log sees these two.
    "sata_slow": "Boot log",
    "can_missing": "Boot log",
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


def fault_ids(run):
    return [fault["id"] for fault in run.boot_faults]


def test_healthy_device_matches_the_golden_log():
    run = run_against("none")
    step = next(step for step in run.steps if step.name == "Boot log")
    assert step.status == "PASS"
    assert run.boot_faults == []
    assert "no deviations" in run.boot_diff


def test_boot_hang_fails_boot_and_gets_a_diagnosis():
    run = run_against("boot_hang")
    result = statuses(run)
    assert [name for name, status in result.items() if status == "FAIL"] == ["Boot", "Boot log"]
    assert fault_ids(run) == ["sata-link", "boot-incomplete"]
    assert result["Idle current"] == "SKIP"
    assert "kernel" not in run.boot_diff.split("Known faults")[0]


def test_slow_sata_link_is_diagnosed():
    run = run_against("sata_slow")
    assert fault_ids(run) == ["sata-link"]
    step = next(step for step in run.steps if step.name == "Boot log")
    assert "SATA link problems" in step.message
    assert "ata1: limiting SATA link speed" in run.boot_faults[0]["lines"][0]


def test_missing_driver_points_at_the_golden_line():
    run = run_against("can_missing")
    assert fault_ids(run) == ["can-missing"]
    assert run.boot_faults[0]["log"] == "golden"
    assert "Missing lines" in {m.name for step in run.steps for m in step.measurements}


def test_short_skips_the_boot_log():
    assert statuses(run_against("short"))["Boot log"] == "SKIP"


def test_no_golden_log_means_no_step():
    config = load_config(_default_config())
    del config["boot"]["golden_log"]
    config["boot"]["timeout_s"] = 2
    with Simulator(time_scale=0.2, ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0)) as resources:
        config["resources"] = resources
        run = EndOfLineTest(config).run()
    assert run.status == "PASS"
    assert "Boot log" not in statuses(run)


def test_golden_log_path_is_relative_to_the_config(tmp_path):
    (tmp_path / "golden.log").write_text("login:\n")
    (tmp_path / "rules.toml").write_text("")
    path = tmp_path / "station.yaml"
    path.write_text("boot:\n  golden_log: golden.log\n  rules: [rules.toml]\n")
    boot = load_config(path)["boot"]
    assert boot["golden_log"] == str(tmp_path / "golden.log")
    assert boot["rules"] == [str(tmp_path / "rules.toml")]


def test_masks_and_ignores_from_the_config():
    config = load_config(_default_config())
    config["boot"]["ignore"] = ["can0"]
    config["boot"]["timeout_s"] = 2
    with Simulator(fault="can_missing", time_scale=0.2,
                   ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0)) as resources:
        config["resources"] = resources
        assert EndOfLineTest(config).run().status == "PASS"


def test_bad_golden_log_stops_before_power_on(tmp_path):
    config = load_config(_default_config())
    config["boot"]["golden_log"] = str(tmp_path / "nope.log")
    with pytest.raises(OSError):
        EndOfLineTest(config)


def test_cli_reports_a_bad_config(tmp_path, capsys):
    path = tmp_path / "station.yaml"
    path.write_text("station: x\nboot:\n  golden_log: nope.log\n")
    assert main(["run", "--config", str(path)]) == 2
    assert "nope.log" in capsys.readouterr().err


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


def test_reports_show_the_diagnosis(tmp_path):
    run = run_against("can_missing")
    folder = write_reports(run, tmp_path)
    data = json.loads((folder / "result.json").read_text())
    assert data["boot_faults"][0]["id"] == "can-missing"
    assert "can0" in data["boot_diff"]
    html = (folder / "report.html").read_text()
    assert "Boot log diagnosis" in html
    assert "CAN interface no longer detected" in html

"""End-of-line test sequence for the embedded PC."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pyvisa
import yaml

from .drivers import ConsoleTimeout, Multimeter, Multiplexer, PowerSupply, SerialConsole

PASS, FAIL, ERROR, SKIP = "PASS", "FAIL", "ERROR", "SKIP"


@dataclass
class Measurement:
    name: str
    value: float | str
    unit: str = ""
    low: float | str | None = None
    high: float | str | None = None

    @property
    def passed(self) -> bool:
        if isinstance(self.value, str) or isinstance(self.low, str):
            return self.low is None or self.value == self.low
        return ((self.low is None or self.value >= self.low)
                and (self.high is None or self.value <= self.high))


@dataclass
class StepResult:
    name: str
    status: str = PASS
    measurements: list[Measurement] = field(default_factory=list)
    message: str = ""
    duration_s: float = 0.0


@dataclass
class TestRun:
    station: str
    started: datetime
    serial_number: str = "UNKNOWN"
    steps: list[StepResult] = field(default_factory=list)
    instruments: dict[str, str] = field(default_factory=dict)
    console_log: str = ""
    duration_s: float = 0.0

    @property
    def status(self) -> str:
        return PASS if self.steps and all(s.status == PASS for s in self.steps) else FAIL


class StepFailed(Exception):
    """Raised inside a step to stop it with a message."""


class Abort(Exception):
    """Raised when continuing could damage the device: skip the rest."""


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class EndOfLineTest:
    def __init__(self, config: dict, on_step: Callable[[StepResult], None] | None = None):
        self.cfg = config
        self.on_step = on_step or (lambda step: None)

    def run(self, serial_number: str | None = None) -> TestRun:
        cfg = self.cfg
        run = TestRun(station=cfg["station"], started=datetime.now().astimezone())
        t0 = time.monotonic()
        rm = pyvisa.ResourceManager(cfg.get("visa_backend", "@py"))
        res = cfg["resources"]
        self.psu = PowerSupply(rm, res["psu"])
        self.dmm = Multimeter(rm, res["dmm"])
        self.mux = Multiplexer(rm, res["mux"])
        self.console = SerialConsole(res["console"], cfg.get("console_baudrate", 115200))
        self.booted = False
        steps = [
            ("Instrument check", self._instruments),
            ("Power on", self._power_on),
            ("Supply rails", self._rails),
            ("Boot", self._boot),
            ("Idle current", self._idle_current),
            ("Identity", self._identity),
            ("Memory", self._memory),
            ("Storage", self._storage),
            ("USB ports", self._usb),
            ("Ethernet", self._ethernet),
            ("Temperature", self._temperature),
            ("RTC", self._rtc),
        ]
        self.result = run
        aborted = False
        try:
            for name, func in steps:
                step = StepResult(name)
                if aborted or (not self.booted and name not in
                               ("Instrument check", "Power on", "Supply rails", "Boot")):
                    step.status, step.message = SKIP, "skipped after earlier failure"
                else:
                    aborted = self._execute(step, func)
                run.steps.append(step)
                self.on_step(step)
        finally:
            self.psu.output(False)
            self.mux.open_all()
            run.console_log = self.console.log
            for closable in (self.console, self.psu, self.dmm, self.mux):
                closable.close()
            rm.close()
        if serial_number:
            run.serial_number = serial_number
        run.duration_s = time.monotonic() - t0
        return run

    def _execute(self, step: StepResult, func: Callable[[StepResult], None]) -> bool:
        """Run one step; return True if the rest of the sequence must be skipped."""
        start = time.monotonic()
        aborted = False
        try:
            func(step)
            if not all(m.passed for m in step.measurements):
                step.status = FAIL
                step.message = step.message or ", ".join(
                    m.name for m in step.measurements if not m.passed) + " out of limits"
        except Abort as exc:
            step.status, step.message, aborted = FAIL, str(exc), True
        except StepFailed as exc:
            step.status, step.message = FAIL, str(exc)
        except Exception as exc:  # instrument or communication problem
            step.status, step.message = ERROR, f"{type(exc).__name__}: {exc}"
            aborted = True
        step.duration_s = time.monotonic() - start
        return aborted

    # -- steps -------------------------------------------------------------

    def _instruments(self, step: StepResult) -> None:
        for name, inst in (("psu", self.psu), ("dmm", self.dmm), ("mux", self.mux)):
            inst.write("*RST")
            self.result.instruments[name] = inst.idn()
            inst.check_errors()
        step.message = "; ".join(self.result.instruments.values())

    def _power_on(self, step: StepResult) -> None:
        p = self.cfg["power"]
        self.psu.configure(p["voltage"], p["current_limit"])
        self.psu.output(True)
        peak, end = 0.0, time.monotonic() + p["inrush_window_s"]
        while time.monotonic() < end:
            peak = max(peak, self.psu.measure_current())
        supply = self.psu.measure_voltage()
        step.measurements += [
            Measurement("Inrush peak", round(peak, 3), "A", high=p["inrush_max_a"]),
            Measurement("Supply voltage", round(supply, 3), "V", low=p["min_supply_v"]),
        ]
        if supply < p["min_supply_v"]:
            self.psu.output(False)
            raise Abort(f"supply collapsed to {supply:.2f} V (current limit) -- "
                        "power removed, possible short circuit")

    def _rails(self, step: StepResult) -> None:
        for rail in self.cfg["rails"]:
            self.mux.close_channel(rail["channel"])
            value = self.dmm.measure_dc_voltage()
            tol = rail["nominal"] * rail["tolerance_pct"] / 100
            step.measurements.append(Measurement(
                rail["name"], round(value, 4), "V",
                low=round(rail["nominal"] - tol, 4), high=round(rail["nominal"] + tol, 4)))
        self.mux.open_all()

    def _boot(self, step: StepResult) -> None:
        b = self.cfg["boot"]
        start = time.monotonic()
        try:
            self.console.read_until(b["login_prompt"], b["timeout_s"])
        except ConsoleTimeout as exc:
            last = exc.captured.strip().splitlines()[-1:] or ["<no output>"]
            raise StepFailed(f"no login prompt within {b['timeout_s']} s, last line: {last[0]}")
        step.measurements.append(Measurement(
            "Boot time", round(time.monotonic() - start, 2), "s", high=b["timeout_s"]))
        self.console.login(b["user"])
        self.booted = True

    def _idle_current(self, step: StepResult) -> None:
        low, high = self.cfg["power"]["idle_current_a"]
        current = sum(self.psu.measure_current() for _ in range(5)) / 5
        step.measurements.append(Measurement("Idle current", round(current, 3), "A", low, high))

    def _identity(self, step: StepResult) -> None:
        d = self.cfg["device"]
        serial = self.console.run("cat /sys/class/dmi/id/product_serial")
        bios = self.console.run("cat /sys/class/dmi/id/bios_version")
        self.result.serial_number = serial
        if not re.fullmatch(d["serial_pattern"], serial):
            raise StepFailed(f"serial number {serial!r} does not match {d['serial_pattern']!r}")
        step.measurements += [Measurement("Serial number", serial),
                              Measurement("BIOS version", bios, low=d["bios_version"])]

    def _memory(self, step: StepResult) -> None:
        out = self.console.run("free -m")
        match = re.search(r"^Mem:\s+(\d+)", out, re.MULTILINE)
        if not match:
            raise StepFailed("could not parse 'free -m'")
        step.measurements.append(Measurement(
            "RAM total", int(match.group(1)), "MB", low=self.cfg["device"]["min_memory_mb"]))

    def _storage(self, step: StepResult) -> None:
        s = self.cfg["device"]["smart"]
        out = self.console.run("smartctl -H -A /dev/sda")
        health = re.search(r"overall-health self-assessment test result: (\w+)", out)

        def attribute(name: str) -> int:
            match = re.search(rf"^\s*\d+\s+{name}\s.*?(\d+)\s*$", out, re.MULTILINE)
            if not match:
                raise StepFailed(f"SMART attribute {name} missing")
            return int(match.group(1))

        step.measurements += [
            Measurement("SMART health", health.group(1) if health else "missing", low="PASSED"),
            Measurement("Reallocated sectors", attribute("Reallocated_Sector_Ct"),
                        high=s["max_reallocated"]),
            Measurement("Pending sectors", attribute("Current_Pending_Sector"),
                        high=s["max_pending"]),
        ]

    def _usb(self, step: StepResult) -> None:
        u = self.cfg["device"]["usb"]
        found = self.console.run("lsusb").count(f"ID {u['vid_pid']}")
        step.measurements.append(Measurement(
            "Test sticks detected", found, low=u["expected_count"], high=u["expected_count"]))

    def _ethernet(self, step: StepResult) -> None:
        e = self.cfg["device"]["ethernet"]
        carrier = int(self.console.run(f"cat /sys/class/net/{e['interface']}/carrier"))
        speed = int(self.console.run(f"cat /sys/class/net/{e['interface']}/speed"))
        step.measurements += [Measurement("Link", carrier, low=1, high=1),
                              Measurement("Speed", speed, "Mbit/s", low=e["min_speed_mbps"])]

    def _temperature(self, step: StepResult) -> None:
        millideg = int(self.console.run("cat /sys/class/thermal/thermal_zone0/temp"))
        step.measurements.append(Measurement(
            "CPU temperature", millideg / 1000, "°C", high=self.cfg["device"]["max_temperature_c"]))

    def _rtc(self, step: StepResult) -> None:
        rtc = datetime.fromisoformat(self.console.run("hwclock -r"))
        drift = abs((rtc - datetime.now().astimezone()).total_seconds())
        step.measurements.append(Measurement(
            "RTC drift", round(drift, 1), "s", high=self.cfg["device"]["max_rtc_drift_s"]))

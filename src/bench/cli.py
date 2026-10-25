"""Command line interface: ``bench simulate``, ``bench run`` and ``bench demo``."""

from __future__ import annotations

import argparse
import signal
import sys
from importlib import resources as pkg_resources
from pathlib import Path

from .report import write_reports
from .sim import FAULTS, Simulator
from .station import EndOfLineTest, StepResult, TestRun, load_config

_COLORS = {"PASS": "\033[32m", "FAIL": "\033[31m", "ERROR": "\033[33m", "SKIP": "\033[90m"}


def _print_step(step: StepResult) -> None:
    color, reset = (_COLORS[step.status], "\033[0m") if sys.stdout.isatty() else ("", "")
    detail = ", ".join(f"{m.name} {m.value}{(' ' + m.unit) if m.unit else ''}"
                       for m in step.measurements[:3])
    if step.message and (step.status != "PASS" or not step.measurements):
        detail = step.message
    print(f"  {color}[{step.status:5}]{reset} {step.name:<18} {detail}")


def _finish(run: TestRun, out: str) -> int:
    folder = write_reports(run, out)
    print(f"\n  Result: {run.status}  ({run.serial_number}, {run.duration_s:.1f} s)")
    print(f"  Report: {folder / 'report.html'}")
    return 0 if run.status == "PASS" else 1


def _default_config() -> str:
    bundled = Path(str(pkg_resources.files("bench"))) / "station.yaml"
    return str(bundled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    faults = ", ".join(FAULTS)

    p_sim = sub.add_parser("simulate", help="run the simulated instruments and device")
    p_sim.add_argument("--fault", default="none", choices=FAULTS, metavar="FAULT", help=faults)
    p_sim.add_argument("--time-scale", type=float, default=1.0)

    p_run = sub.add_parser("run", help="run the test against the instruments in the config")
    p_run.add_argument("--config", default=_default_config())
    p_run.add_argument("--serial", help="override the serial number read from the device")
    p_run.add_argument("--out", default="reports")

    p_demo = sub.add_parser("demo", help="start the simulator and run the test against it")
    p_demo.add_argument("--fault", default="none", choices=FAULTS, metavar="FAULT", help=faults)
    p_demo.add_argument("--config", default=_default_config())
    p_demo.add_argument("--out", default="reports")

    args = parser.parse_args(argv)

    if args.cmd == "simulate":
        sim = Simulator(fault=args.fault, time_scale=args.time_scale)
        for name, resource in sim.start().items():
            print(f"  {name:8} {resource}")
        print(f"  fault: {args.fault} ({FAULTS[args.fault]}) -- Ctrl+C to stop")
        try:
            signal.pause()
        except KeyboardInterrupt:
            pass
        sim.stop()
        return 0

    config = load_config(args.config)
    if args.cmd == "demo":
        with Simulator(fault=args.fault, ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0)) as res:
            config["resources"] = res
            print(f"  Simulated device: {FAULTS[args.fault]}\n")
            run = EndOfLineTest(config, on_step=_print_step).run()
        return _finish(run, args.out)

    run = EndOfLineTest(config, on_step=_print_step).run(serial_number=args.serial)
    return _finish(run, args.out)


if __name__ == "__main__":
    sys.exit(main())

# Changelog

## 0.4.0 — 2026-11-26

- Every run also writes a printable test protocol, `protocol.html`, made with
  [test-protocol](https://github.com/steitec/test-protocol): verdict, failed
  checks on top, every measurement with its limits, console log, SHA-256 of
  `result.json` and signature boxes. Set it up in the new `protocol` section of
  `station.yaml` (product, company, logo, language, approver box); leave the
  section out to skip it.
- `bench run` and `bench demo` take `--operator NAME` for the protocol and
  `--pdf` for a `protocol.pdf` (WeasyPrint or Chrome/Chromium). If the PDF
  cannot be made, the run's verdict and exit code stay the same.
- `bench --version`.
- CI also runs on Windows and macOS.
- New dependency: test-protocol 0.1.3 or later (pure Python).

## 0.3.0 — 2026-11-05

- Power-on captured with an oscilloscope, armed before the supply switches on:
  peak inrush current, rail level and ramp time, both curves in the report.
- New faults `inrush_spike` and `rail_slow`, which only the scope catches.

## 0.2.0 — 2026-11-05

- "Boot log" step: the console output of each boot is compared with a golden
  log using [boot-log-diff](https://github.com/steitec/boot-log-diff), with a
  diagnosis of known faults in the report, also when the boot hangs.
- New faults `sata_slow` and `can_missing`.

## 0.1.0 — 2026-10-27

- End-of-line test of a simulated 12 V embedded PC: inrush and idle current,
  supply rails through a relay multiplexer and a DMM, boot over the serial
  console, on-device checks (identity, RAM, SSD health, USB, Ethernet,
  temperature, RTC); JSON, CSV and HTML report per unit.
- Simulated instruments from [scpi-sim](https://github.com/steitec/scpi-sim).

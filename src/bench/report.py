"""Write a test run as JSON, CSV and a self-contained HTML report."""

from __future__ import annotations

import csv
import html
import json
from dataclasses import asdict
from pathlib import Path

from .station import TestRun

_STATUS_COLORS = {"PASS": "#1a7f37", "FAIL": "#cf222e", "ERROR": "#9a6700", "SKIP": "#6e7781"}


def write_reports(run: TestRun, out_dir: str | Path) -> Path:
    stamp = run.started.strftime("%Y%m%d-%H%M%S")
    folder = Path(out_dir) / f"{run.serial_number}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)

    data = asdict(run)
    data["started"] = run.started.isoformat()
    data["status"] = run.status
    for step, raw in zip(run.steps, data["steps"]):
        for m, raw_m in zip(step.measurements, raw["measurements"]):
            raw_m["passed"] = m.passed
    (folder / "result.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                        encoding="utf-8")

    with open(folder / "measurements.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["serial", "step", "measurement", "value", "unit", "low", "high", "result"])
        for step in run.steps:
            for m in step.measurements:
                writer.writerow([run.serial_number, step.name, m.name, m.value, m.unit,
                                 "" if m.low is None else m.low,
                                 "" if m.high is None else m.high,
                                 "PASS" if m.passed else "FAIL"])

    (folder / "report.html").write_text(_html(run), encoding="utf-8")
    return folder


def _badge(status: str) -> str:
    return f'<span class="badge" style="background:{_STATUS_COLORS[status]}">{status}</span>'


def _gauge(value: float, low: float, high: float) -> str:
    """Small inline SVG showing where a value sits inside its tolerance band."""
    span = high - low
    lo, hi = low - span * 0.5, high + span * 0.5
    def x(v: float) -> float:
        return 4 + 152 * (min(max(v, lo), hi) - lo) / (hi - lo)
    color = _STATUS_COLORS["PASS" if low <= value <= high else "FAIL"]
    return (f'<svg width="160" height="14" role="img" aria-label="{value} within {low}..{high}">'
            f'<rect x="4" y="5" width="152" height="4" rx="2" fill="var(--track)"/>'
            f'<rect x="{x(low):.1f}" y="3" width="{x(high) - x(low):.1f}" height="8" rx="2" '
            f'fill="var(--band)"/>'
            f'<circle cx="{x(value):.1f}" cy="7" r="4.5" fill="{color}"/></svg>')


def _limits(low: object, high: object) -> str:
    if isinstance(low, str):
        return f"= {html.escape(low)}"
    if low is not None and high is not None:
        return f"{low} … {high}" if low != high else f"= {low}"
    if low is not None:
        return f"≥ {low}"
    if high is not None:
        return f"≤ {high}"
    return "—"


def _html(run: TestRun) -> str:
    rows = []
    for step in run.steps:
        rows.append(f'<tr class="step"><td colspan="5"><strong>{html.escape(step.name)}</strong> '
                    f'{_badge(step.status)} <span class="muted">{step.duration_s:.2f} s'
                    f'{" — " + html.escape(step.message) if step.message else ""}</span></td></tr>')
        for m in step.measurements:
            numeric = (isinstance(m.value, (int, float)) and isinstance(m.low, (int, float))
                       and isinstance(m.high, (int, float)) and m.low != m.high)
            rows.append(
                f'<tr><td class="indent">{html.escape(m.name)}</td>'
                f'<td class="num">{html.escape(str(m.value))} {html.escape(m.unit)}</td>'
                f'<td class="num">{_limits(m.low, m.high)}</td>'
                f'<td>{_gauge(float(m.value), m.low, m.high) if numeric else ""}</td>'  # type: ignore[arg-type]
                f'<td>{_badge("PASS" if m.passed else "FAIL")}</td></tr>')
    instruments = "".join(f"<li><code>{html.escape(k)}</code> {html.escape(v)}</li>"
                          for k, v in run.instruments.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test report {html.escape(run.serial_number)}</title>
<style>
:root {{ --bg:#fff; --fg:#1f2328; --muted:#656d76; --line:#d0d7de; --track:#d0d7de; --band:#aceebb; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0d1117; --fg:#e6edf3; --muted:#8d96a0; --line:#30363d; --track:#30363d; --band:#2b5a37; }}
}}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif;
       max-width:960px; margin:2rem auto; padding:0 16px; }}
h1 {{ margin-bottom:.2rem; }}
.muted {{ color:var(--muted); }}
.badge {{ color:#fff; border-radius:4px; padding:1px 7px; font-size:12px; font-weight:600; }}
.verdict {{ font-size:22px; padding:4px 14px; }}
table {{ width:100%; border-collapse:collapse; margin:1.5rem 0; }}
td {{ border-bottom:1px solid var(--line); padding:6px 8px; vertical-align:middle; }}
tr.step td {{ padding-top:14px; }}
.indent {{ padding-left:24px; }}
.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
pre {{ background:rgba(127,127,127,.1); padding:12px; overflow-x:auto; font-size:12.5px; }}
</style></head><body>
<h1>End-of-line test {_badge(run.status).replace('class="badge"', 'class="badge verdict"')}</h1>
<p class="muted">Serial <strong>{html.escape(run.serial_number)}</strong> · station
{html.escape(run.station)} · {run.started:%Y-%m-%d %H:%M:%S} · {run.duration_s:.1f} s</p>
<table>{''.join(rows)}</table>
<h2>Instruments</h2><ul>{instruments}</ul>
<details><summary>Console log</summary><pre>{html.escape(run.console_log)}</pre></details>
</body></html>
"""

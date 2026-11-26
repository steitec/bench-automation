"""Printable test protocol of one unit, made with test-protocol from result.json."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from test_protocol.cli import main as test_protocol

_OPTIONS = ("product", "company", "title", "lang")


def write_protocol(folder: Path, settings: dict, operator: str | None = None,
                   pdf: bool = False) -> list[Path]:
    """Write protocol.html (and protocol.pdf) next to result.json in ``folder``.

    ``settings`` is the ``protocol`` section of the station config. Raises
    RuntimeError if test-protocol cannot write the files; an HTML protocol
    written before a PDF error stays.
    """
    folder = Path(folder)
    argv = [str(folder / "result.json"), "--html", str(folder / "protocol.html")]
    for key in _OPTIONS:
        if settings.get(key):
            argv += [f"--{key}", str(settings[key])]
    if settings.get("logo"):
        argv += ["--logo", str(settings["logo"])]
    if settings.get("approver"):
        argv.append("--approver")
    if operator:
        argv += ["--operator", operator]
    if pdf:
        argv += ["--pdf", str(folder / "protocol.pdf")]

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = test_protocol(argv)
    if code == 2:  # 0 and 1 are the verdict, 2 is an error
        message = err.getvalue().strip().removeprefix("test-protocol: error: ")
        raise RuntimeError(message or "test-protocol failed")
    return [folder / name for name in ("protocol.html", "protocol.pdf")
            if (folder / name).exists()]

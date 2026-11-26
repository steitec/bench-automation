import pytest
import yaml

from bench.cli import _default_config, main
from bench.protocol import write_protocol
from bench.report import write_reports
from bench.station import load_config

from test_station import run_against

SETTINGS = {"product": "EPC-24 panel PC", "lang": "en", "approver": True}


def test_protocol_of_a_good_unit(tmp_path):
    folder = write_reports(run_against("none"), tmp_path)
    written = write_protocol(folder, SETTINGS, operator="J. Doe")
    assert written == [folder / "protocol.html"]
    html = written[0].read_text(encoding="utf-8")
    assert "EPC-24-00137" in html
    assert "EPC-24 panel PC" in html
    assert "J. Doe" in html
    assert "result.json" in html  # traceability: the input and its SHA-256
    assert ">PASS<" in html


def test_protocol_lists_the_failed_checks(tmp_path):
    folder = write_reports(run_against("smart"), tmp_path)
    html = write_protocol(folder, SETTINGS)[0].read_text(encoding="utf-8")
    assert ">FAIL<" in html
    assert "Reallocated sectors" in html


def test_protocol_in_german(tmp_path):
    folder = write_reports(run_against("none"), tmp_path)
    html = write_protocol(folder, {"lang": "de"})[0].read_text(encoding="utf-8")
    assert "Prüfprotokoll" in html


def test_pdf_error_keeps_the_html(tmp_path, monkeypatch):
    import test_protocol.cli as tp_cli

    def no_engine(*_args, **_kwargs):
        raise tp_cli.PdfError("no PDF engine found")

    monkeypatch.setattr(tp_cli, "write_pdf", no_engine)
    folder = write_reports(run_against("none"), tmp_path)
    with pytest.raises(RuntimeError, match="no PDF engine"):
        write_protocol(folder, SETTINGS, pdf=True)
    assert (folder / "protocol.html").exists()


def test_demo_writes_the_protocol(tmp_path, capsys):
    assert main(["demo", "--out", str(tmp_path), "--operator", "J. Doe"]) == 0
    out = capsys.readouterr().out
    assert "Protocol:" in out
    [folder] = tmp_path.iterdir()
    assert (folder / "protocol.html").exists()


def test_no_protocol_section_means_no_protocol(tmp_path, capsys):
    config = load_config(_default_config())  # file paths in it are now absolute
    del config["protocol"]
    path = tmp_path / "station.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert main(["demo", "--config", str(path), "--out", str(tmp_path / "r")]) == 0
    assert "Protocol:" not in capsys.readouterr().out
    [folder] = (tmp_path / "r").iterdir()
    assert not (folder / "protocol.html").exists()

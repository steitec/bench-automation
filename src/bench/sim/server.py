"""TCP servers that make the simulated instruments look like LAN instruments.

The power supply, multimeter and multiplexer speak line-based SCPI on raw
sockets (like port 5025 on real instruments). The serial console is exposed
the way a ser2net / terminal server would expose a real RS-232 port.
"""

from __future__ import annotations

import re
import socketserver
import threading

from .dut import EmbeddedPC

# Long SCPI keywords -> short form, so both "MEASure:VOLTage?" spellings work.
_LONG_FORMS = {
    "SOURCE": "SOUR", "VOLTAGE": "VOLT", "CURRENT": "CURR", "OUTPUT": "OUTP",
    "MEASURE": "MEAS", "SYSTEM": "SYST", "ERROR": "ERR", "ROUTE": "ROUT",
    "CLOSE": "CLOS", "LEVEL": "LEV", "STATE": "STAT",
}


def _normalize(header: str) -> str:
    query = header.endswith("?")
    tokens = header.rstrip("?").upper().split(":")
    tokens = [_LONG_FORMS.get(t, t) for t in tokens if t]
    tokens = [t for t in tokens if t not in ("SOUR", "LEV", "STAT")]
    return ":".join(tokens) + ("?" if query else "")


def _channels(arg: str) -> list[int]:
    match = re.fullmatch(r"\(@([\d,:\s]+)\)", arg.strip())
    if not match:
        raise ValueError(arg)
    channels: list[int] = []
    for part in match.group(1).split(","):
        if ":" in part:
            first, last = (int(x) for x in part.split(":"))
            channels.extend(range(first, last + 1))
        else:
            channels.append(int(part))
    return channels


class SimInstrument:
    idn = "BenchSim,GENERIC,SIM0000,1.0"

    def __init__(self, dut: EmbeddedPC):
        self.dut = dut
        self.errors: list[str] = []

    def handle(self, command: str) -> str | None:
        header, _, arg = command.strip().partition(" ")
        header = _normalize(header)
        if header == "*IDN?":
            return self.idn
        if header == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if header in ("*RST", "*CLS"):
            self.reset()
            return None
        if header == "*OPC?":
            return "1"
        try:
            return self.command(header, arg.strip())
        except KeyError:
            self.errors.append('-113,"Undefined header"')
        except ValueError:
            self.errors.append('-224,"Illegal parameter value"')
        return None

    def reset(self) -> None:
        self.errors.clear()

    def command(self, header: str, arg: str) -> str | None:
        raise KeyError(header)


class SimPowerSupply(SimInstrument):
    idn = "BenchSim,PSU-3005,SIM0001,1.0"

    def reset(self) -> None:
        super().reset()
        self.dut.set_output(False)
        self.dut.psu_voltage, self.dut.psu_current_limit = 0.0, 1.0

    def command(self, header: str, arg: str) -> str | None:
        dut = self.dut
        if header == "VOLT":
            value = float(arg)
            if not 0 <= value <= 30:
                raise ValueError(arg)
            dut.psu_voltage = value
        elif header == "VOLT?":
            return f"{dut.psu_voltage:.3f}"
        elif header == "CURR":
            value = float(arg)
            if not 0 <= value <= 5:
                raise ValueError(arg)
            dut.psu_current_limit = value
        elif header == "CURR?":
            return f"{dut.psu_current_limit:.3f}"
        elif header == "OUTP":
            if arg.upper() not in ("ON", "OFF", "1", "0"):
                raise ValueError(arg)
            dut.set_output(arg.upper() in ("ON", "1"))
        elif header == "OUTP?":
            return "1" if dut.psu_output else "0"
        elif header == "MEAS:VOLT?":
            return f"{dut.output()[0]:.4f}"
        elif header == "MEAS:CURR?":
            return f"{dut.output()[1]:.4f}"
        else:
            raise KeyError(header)
        return None


class SimMultimeter(SimInstrument):
    idn = "BenchSim,DMM-34461,SIM0002,1.0"

    def command(self, header: str, arg: str) -> str | None:
        if header in ("MEAS:VOLT:DC?", "MEAS:VOLT?"):
            return f"{self.dut.dmm_voltage():+.6E}"
        raise KeyError(header)


class SimMultiplexer(SimInstrument):
    idn = "BenchSim,MUX-34903,SIM0003,1.0"

    def reset(self) -> None:
        super().reset()
        self.dut.mux_closed.clear()

    def command(self, header: str, arg: str) -> str | None:
        closed = self.dut.mux_closed
        if header == "ROUT:CLOS":
            closed.update(_channels(arg))
        elif header == "ROUT:OPEN":
            closed.difference_update(_channels(arg))
        elif header == "ROUT:OPEN:ALL":
            closed.clear()
        elif header == "ROUT:CLOS?":
            return ",".join("1" if ch in closed else "0" for ch in _channels(arg))
        else:
            raise KeyError(header)
        return None


class _ScpiHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        instrument: SimInstrument = self.server.instrument  # type: ignore[attr-defined]
        for raw in self.rfile:
            for command in raw.decode(errors="replace").strip().split(";"):
                if not command.strip():
                    continue
                reply = instrument.handle(command)
                if reply is not None:
                    self.wfile.write((reply + "\n").encode())


class _ConsoleHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        dut: EmbeddedPC = self.server.dut  # type: ignore[attr-defined]
        lock = threading.Lock()

        def sink(text: str) -> None:
            with lock:
                try:
                    self.wfile.write(text.encode())
                except OSError:
                    pass

        dut.attach_console(sink)
        try:
            for raw in self.rfile:
                dut.console_input(raw.decode(errors="replace").rstrip("\r\n"))
        finally:
            dut.detach_console(sink)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Simulator:
    """Runs all simulated instruments for one device under test."""

    DEFAULT_PORTS = {"psu": 5025, "dmm": 5026, "mux": 5027, "console": 5028}

    def __init__(self, fault: str = "none", time_scale: float = 1.0,
                 host: str = "127.0.0.1", ports: dict[str, int] | None = None):
        self.dut = EmbeddedPC(fault=fault, time_scale=time_scale)
        self.host = host
        self.ports = dict(self.DEFAULT_PORTS if ports is None else ports)
        self._servers: list[_Server] = []

    def start(self) -> dict[str, str]:
        """Start all servers and return the resource strings to reach them."""
        instruments = {"psu": SimPowerSupply, "dmm": SimMultimeter, "mux": SimMultiplexer}
        resources = {}
        for name, cls in instruments.items():
            server = _Server((self.host, self.ports[name]), _ScpiHandler)
            server.instrument = cls(self.dut)  # type: ignore[attr-defined]
            self._serve(server)
            resources[name] = f"TCPIP0::{self.host}::{server.server_address[1]}::SOCKET"
        console = _Server((self.host, self.ports["console"]), _ConsoleHandler)
        console.dut = self.dut  # type: ignore[attr-defined]
        self._serve(console)
        resources["console"] = f"socket://{self.host}:{console.server_address[1]}"
        return resources

    def _serve(self, server: _Server) -> None:
        self._servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.dut.set_output(False)
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers.clear()

    def __enter__(self) -> dict[str, str]:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

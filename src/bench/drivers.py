"""Instrument drivers.

The SCPI drivers go through PyVISA, so the same code talks to the simulator
(``TCPIP0::127.0.0.1::5025::SOCKET``) or to real hardware (``USB0::...``,
``TCPIP0::192.168.1.20::INSTR``, ``ASRL/dev/ttyUSB0::INSTR``) -- only the
resource string in the station config changes.
"""

from __future__ import annotations

import re
import time

import pyvisa
import serial


class InstrumentError(RuntimeError):
    pass


class ScpiInstrument:
    def __init__(self, rm: pyvisa.ResourceManager, resource: str, timeout_ms: int = 3000):
        self.resource = resource
        self._inst = rm.open_resource(resource, read_termination="\n",
                                      write_termination="\n", timeout=timeout_ms)

    def write(self, command: str) -> None:
        self._inst.write(command)

    def query(self, command: str) -> str:
        return self._inst.query(command).strip()

    def query_float(self, command: str) -> float:
        return float(self.query(command))

    def idn(self) -> str:
        return self.query("*IDN?")

    def check_errors(self) -> None:
        """Raise if the instrument queued any SCPI errors."""
        errors = []
        while not (err := self.query("SYST:ERR?")).startswith(("0,", "+0,")):
            errors.append(err)
        if errors:
            raise InstrumentError(f"{self.resource}: {'; '.join(errors)}")

    def close(self) -> None:
        self._inst.close()


class PowerSupply(ScpiInstrument):
    def configure(self, voltage: float, current_limit: float) -> None:
        self.write(f"VOLT {voltage:.3f}")
        self.write(f"CURR {current_limit:.3f}")
        self.check_errors()

    def output(self, on: bool) -> None:
        self.write(f"OUTP {'ON' if on else 'OFF'}")

    def measure_voltage(self) -> float:
        return self.query_float("MEAS:VOLT?")

    def measure_current(self) -> float:
        return self.query_float("MEAS:CURR?")


class Multimeter(ScpiInstrument):
    def measure_dc_voltage(self) -> float:
        return self.query_float("MEAS:VOLT:DC?")


class Multiplexer(ScpiInstrument):
    def close_channel(self, channel: int) -> None:
        self.write("ROUT:OPEN:ALL")
        self.write(f"ROUT:CLOS (@{channel})")
        if self.query(f"ROUT:CLOS? (@{channel})") != "1":
            raise InstrumentError(f"{self.resource}: channel {channel} did not close")

    def open_all(self) -> None:
        self.write("ROUT:OPEN:ALL")


class ConsoleTimeout(TimeoutError):
    def __init__(self, message: str, captured: str):
        super().__init__(message)
        self.captured = captured


class SerialConsole:
    """Serial console of the device under test.

    ``url`` is anything pyserial understands: ``/dev/ttyUSB0``, ``COM3`` or
    ``socket://host:port`` for a terminal server (and the simulator).
    """

    PROMPT = r"\n# $"

    def __init__(self, url: str, baudrate: int = 115200):
        self._port = serial.serial_for_url(url, baudrate=baudrate, timeout=0.05)
        self.log = ""  # everything received, kept for the report

    def read_until(self, pattern: str, timeout: float) -> str:
        """Read until ``pattern`` matches; return the text read in this call."""
        regex = re.compile(pattern)
        buffer = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._port.read(4096).decode(errors="replace").replace("\r", "")
            if chunk:
                buffer += chunk
                self.log += chunk
                if regex.search(buffer):
                    return buffer
        raise ConsoleTimeout(f"no match for {pattern!r} within {timeout:.1f} s", buffer)

    def login(self, user: str, timeout: float = 5.0) -> None:
        self._port.write(f"{user}\n".encode())
        self.read_until(self.PROMPT, timeout)

    def run(self, command: str, timeout: float = 5.0) -> str:
        """Run a shell command and return its output without echo and prompt."""
        self._port.reset_input_buffer()
        self._port.write(f"{command}\n".encode())
        text = self.read_until(self.PROMPT, timeout)
        lines = text.split("\n")
        if lines and lines[0].strip() == command:
            lines = lines[1:]
        return "\n".join(lines[:-1]).strip()

    def close(self) -> None:
        self._port.close()

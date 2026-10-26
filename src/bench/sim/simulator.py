"""The simulated test bench: scpi-sim instruments wired to the EmbeddedPC.

    supply ch1 ──── dut.vin
    mux 101..104 ── dut.5V0 / 3V3 / 1V8 / 1V05
    mux common ──── dmm input
    dut console ─── TCP port (like a terminal server)
"""

from __future__ import annotations

from scpi_sim import Bench

from .dut import FAULTS, MUX_WIRING


class Simulator:
    """Runs the simulated instruments and the device under test."""

    DEFAULT_PORTS = {"psu": 5025, "dmm": 5026, "mux": 5027, "console": 5028}

    def __init__(self, fault: str = "none", time_scale: float = 1.0,
                 host: str = "127.0.0.1", ports: dict[str, int] | None = None):
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}, choose from {sorted(FAULTS)}")
        self.host = host
        self.ports = dict(self.DEFAULT_PORTS if ports is None else ports)
        self.bench = Bench.from_dict({
            "instruments": {
                "psu": {"type": "psu", "port": self.ports["psu"], "max_amps": 5},
                "dmm": {"type": "dmm", "port": self.ports["dmm"]},
                "mux": {"type": "mux", "port": self.ports["mux"]},
            },
            "devices": {
                "dut": {"class": "bench.sim.dut:EmbeddedPC",
                        "args": {"fault": fault, "time_scale": time_scale},
                        "console": self.ports["console"]},
            },
            "wiring": [
                "psu.ch1 -> dut.vin",
                *(f"mux.{channel} -> dut.{rail}" for channel, rail in MUX_WIRING.items()),
                "mux.common -> dmm.input",
            ],
        })
        self.dut = self.bench.devices["dut"]
        self.resources: dict[str, str] = {}

    def start(self) -> dict[str, str]:
        """Start all servers and return the resource strings to reach them."""
        self.bench.start(host=self.host)
        self.resources = {name: self.bench.resource(name) for name in ("psu", "dmm", "mux")}
        self.resources["console"] = self.bench.console_url("dut")
        return self.resources

    def stop(self) -> None:
        self.dut.power_off()
        self.bench.stop()

    def __enter__(self) -> dict[str, str]:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

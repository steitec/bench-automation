"""Behavioural model of the device under test: a 12 V embedded PC.

The model is shared by all simulated instruments, so the power supply, the
multiplexer, the multimeter and the serial console all see the same device.
Timings are given in "device seconds" and divided by ``time_scale`` -- a scale
of 0.2 runs the device five times faster, which keeps the test suite quick.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Callable

FAULTS = {
    "none": "healthy device",
    "short": "short circuit on the 12 V input",
    "rail_1v05": "1.05 V core rail missing",
    "boot_hang": "boot hangs in the kernel (SATA reset loop)",
    "usb_port": "one USB port dead",
    "smart": "SSD with reallocated and pending sectors",
    "no_link": "no Ethernet link",
    "overtemp": "CPU running hot (missing thermal pad)",
}

RAILS = {"5V0": 5.0, "3V3": 3.3, "1V8": 1.8, "1V05": 1.05}
# Which rail each multiplexer channel is wired to on the test fixture.
MUX_WIRING = {101: "5V0", 102: "3V3", 103: "1V8", 104: "1V05"}

INRUSH_END_S = 0.3
BOOT_DONE_S = 2.5
MIN_INPUT_V = 10.8

BOOT_LOG = [
    (0.4, "AMI BIOS v5.27 (C) 2024 BenchSim Embedded"),
    (0.5, "POST: CPU Intel Atom x6425E @ 2.00GHz ... OK"),
    (0.6, "POST: Memory 8192 MB DDR4 ... OK"),
    (0.7, "POST: SATA0 SSD 128GB ... OK"),
    (1.2, "Booting from SATA0..."),
    (1.3, "Loading Linux 6.6.0-epc ..."),
    (1.6, "[    0.000000] Linux version 6.6.0-epc (build@ci) #1 SMP PREEMPT_DYNAMIC"),
    (1.9, "[    1.204511] ata1: SATA link up 6.0 Gbps (SStatus 133 SControl 300)"),
    (2.1, "[    1.873320] e1000e 0000:00:1f.6 eth0: registered PHC clock"),
    (2.3, "[    2.410552] systemd[1]: Reached target multi-user.target"),
]
HANG_LOG = [
    (1.9, "[    1.204511] ata1: COMRESET failed (errno=-16)"),
    (2.6, "[    6.311045] ata1: COMRESET failed (errno=-16)"),
    (3.3, "[   11.418871] ata1: limiting SATA link speed to 3.0 Gbps"),
]


class EmbeddedPC:
    def __init__(self, fault: str = "none", time_scale: float = 1.0,
                 serial_number: str = "EPC-24-00137"):
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}, choose from {sorted(FAULTS)}")
        self.fault = fault
        self.time_scale = time_scale
        self.serial_number = serial_number
        self.psu_voltage = 0.0
        self.psu_current_limit = 1.0
        self.psu_output = False
        self.mux_closed: set[int] = set()
        self.state = "off"  # off -> booting -> login -> shell
        self._power_on_at = 0.0
        self._generation = 0
        self._lock = threading.Lock()
        self._console_sinks: list[Callable[[str], None]] = []
        self._rng = random.Random()

    # -- power -------------------------------------------------------------

    def set_output(self, on: bool) -> None:
        with self._lock:
            if on == self.psu_output:
                return
            self.psu_output = on
            self._generation += 1
            generation = self._generation
            if on:
                self._power_on_at = time.monotonic()
                self.state = "booting"
            else:
                self.state = "off"
        if on:
            threading.Thread(target=self._boot, args=(generation,), daemon=True).start()

    def _elapsed(self) -> float:
        return (time.monotonic() - self._power_on_at) / self.time_scale

    def _load_current(self) -> float:
        """Current the device would draw without a current limit."""
        if not self.psu_output:
            return 0.0
        if self.fault == "short":
            return 4.5
        if self.psu_voltage < MIN_INPUT_V:
            return 0.05
        t = self._elapsed()
        if t < INRUSH_END_S:
            amps = 2.2
        elif t < BOOT_DONE_S or self.fault == "boot_hang":
            amps = 1.3
        else:
            amps = 0.85
        return amps + self._rng.gauss(0, 0.015)

    def output(self) -> tuple[float, float]:
        """Voltage and current at the supply terminals (constant-current aware)."""
        if not self.psu_output:
            return 0.0, 0.0
        load = self._load_current()
        if load > self.psu_current_limit:
            return self.psu_voltage * self.psu_current_limit / load, self.psu_current_limit
        return self.psu_voltage, load

    def input_ok(self) -> bool:
        return self.output()[0] >= MIN_INPUT_V

    # -- measurement through the fixture -----------------------------------

    def rail_voltage(self, rail: str) -> float:
        if not self.input_ok() or (rail == "1V05" and self.fault == "rail_1v05"):
            return abs(self._rng.gauss(0.02, 0.005))
        return RAILS[rail] * (1 + self._rng.gauss(0, 0.003))

    def dmm_voltage(self) -> float:
        rails = [MUX_WIRING[ch] for ch in self.mux_closed if ch in MUX_WIRING]
        if len(rails) != 1:
            return self._rng.gauss(0, 0.0005)  # open input, just noise
        return self.rail_voltage(rails[0])

    # -- serial console ----------------------------------------------------

    def attach_console(self, sink: Callable[[str], None]) -> None:
        self._console_sinks.append(sink)

    def detach_console(self, sink: Callable[[str], None]) -> None:
        if sink in self._console_sinks:
            self._console_sinks.remove(sink)

    def _emit(self, text: str) -> None:
        for sink in list(self._console_sinks):
            sink(text)

    def _boot(self, generation: int) -> None:
        if not self.input_ok():
            return  # brown-out: the board never starts
        log = BOOT_LOG[:6] + HANG_LOG if self.fault == "boot_hang" else BOOT_LOG
        for at, line in log:
            delay = self._power_on_at + at * self.time_scale - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if generation != self._generation:
                return
            self._emit(line + "\n")
        if self.fault == "boot_hang":
            return
        delay = self._power_on_at + BOOT_DONE_S * self.time_scale - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if generation == self._generation:
            self.state = "login"
            self._emit("\nepc-test login: ")

    def console_input(self, line: str) -> None:
        """Handle one line typed on the console (the console echoes it)."""
        if self.state not in ("login", "shell"):
            return
        self._emit(line + "\n")
        if self.state == "login":
            if line == "root":
                self.state = "shell"
                self._emit("Last login: on ttyS0\n# ")
            else:
                self._emit("Login incorrect\n\nepc-test login: ")
            return
        output = self._shell(line.strip())
        self._emit((output + "\n" if output else "") + "# ")

    def _shell(self, cmd: str) -> str:
        if not cmd:
            return ""
        if cmd == "cat /sys/class/dmi/id/product_serial":
            return self.serial_number
        if cmd == "cat /sys/class/dmi/id/bios_version":
            return "5.27"
        if cmd == "free -m":
            return ("               total        used        free      shared  buff/cache   available\n"
                    "Mem:            7843         312        7201           9         329        7531\n"
                    "Swap:              0           0           0")
        if cmd == "smartctl -H -A /dev/sda":
            realloc, pending = (312, 24) if self.fault == "smart" else (0, 0)
            return ("=== START OF READ SMART DATA SECTION ===\n"
                    "SMART overall-health self-assessment test result: PASSED\n\n"
                    "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE\n"
                    f"  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       {realloc}\n"
                    "  9 Power_On_Hours          0x0032   100   100   000    Old_age   Always       -       3\n"
                    f"197 Current_Pending_Sector  0x0032   100   100   000    Old_age   Always       -       {pending}")
        if cmd == "lsusb":
            ports = 3 if self.fault == "usb_port" else 4
            lines = ["Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub"]
            lines += [f"Bus 001 Device {n + 2:03d}: ID 0781:5567 SanDisk Corp. Cruzer Blade"
                      for n in range(ports)]
            return "\n".join(lines)
        if cmd == "cat /sys/class/net/eth0/carrier":
            return "0" if self.fault == "no_link" else "1"
        if cmd == "cat /sys/class/net/eth0/speed":
            return "-1" if self.fault == "no_link" else "1000"
        if cmd == "cat /sys/class/thermal/thermal_zone0/temp":
            return "91000" if self.fault == "overtemp" else "46500"
        if cmd == "hwclock -r":
            return datetime.now().astimezone().isoformat(sep=" ")
        return f"sh: {cmd.split()[0]}: not found"

"""Behavioural model of the device under test: a 12 V embedded PC.

It is a scpi-sim device: its 12 V input ``vin`` is wired to the simulated power
supply, its four rails to the multiplexer, and its serial console is a TCP port
of the bench. The supply asks it for its current, the multimeter for its rail
voltages, and after every instrument command it checks whether its power
changed. Timings are given in "device seconds" and divided by ``time_scale`` --
a scale of 0.2 runs the device five times faster, which keeps the test suite quick.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime

from scpi_sim import Device

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

SHORT_OHMS = 12.0 / 4.5  # the shorted input draws 4.5 A at 12 V
POWER_PRESENT_V = 0.5    # any input voltage above this counts as "power applied"

INRUSH_END_S = 0.3
BOOT_DONE_S = 2.5
MIN_INPUT_V = 10.8
NOISE_TICK_S = 0.01  # real seconds the load noise stays the same (one supply measurement)

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


class EmbeddedPC(Device):
    terminals = {"vin": "input", **{rail: "output" for rail in RAILS}}

    def __init__(self, fault: str = "none", time_scale: float = 1.0,
                 serial_number: str = "EPC-24-00137"):
        super().__init__()
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}, choose from {sorted(FAULTS)}")
        self.fault = fault
        self.time_scale = time_scale
        self.serial_number = serial_number
        self.state = "off"  # off -> booting -> login -> shell
        self._power_on_at: float | None = None
        self._undervoltage = False  # input below MIN_INPUT_V when power was applied: never starts
        self._generation = 0
        self._lock = threading.Lock()
        self._rng = random.Random()
        self._noise = (0.0, -NOISE_TICK_S)  # (amps, time.monotonic() when drawn)

    # -- power (called by the bench after every instrument command) ----------

    def update(self) -> None:
        vin = self.input_voltage("vin")  # while off the board draws only standby current
        powered = vin > POWER_PRESENT_V
        with self._lock:
            if powered == (self._power_on_at is not None):
                return
            self._generation += 1
            generation = self._generation
            if powered:
                self._undervoltage = vin < MIN_INPUT_V
                self._power_on_at = time.monotonic()
                self.state = "booting"
            else:
                self._power_on_at = None
                self.state = "off"
        if powered:
            threading.Thread(target=self._boot, args=(generation, self._power_on_at), daemon=True).start()

    def power_off(self) -> None:
        """Forget the power state (stops a running boot), e.g. when the simulator stops."""
        with self._lock:
            self._generation += 1
            self._power_on_at = None
            self.state = "off"

    def _elapsed(self, power_on_at: float) -> float:
        return (time.monotonic() - power_on_at) / self.time_scale

    def current(self, terminal: str, volts: float) -> float:
        """Current drawn at the 12 V input.

        A running board behaves like a resistive load sized for 12 V, so a
        current limit below its demand pulls the supply down to
        12 V x limit / demand -- a deep sag then browns the board out.
        """
        if volts <= 0:
            return 0.0
        if self.fault == "short":
            return volts / SHORT_OHMS
        power_on_at = self._power_on_at  # read once: update() runs in other threads
        if power_on_at is None or self._undervoltage:
            return 0.05  # standby / under-voltage lockout
        t = self._elapsed(power_on_at)
        if t < INRUSH_END_S:
            return 2.2 * volts / 12.0  # charging the input capacitors: no noise
        if t < BOOT_DONE_S or self.fault == "boot_hang":
            amps = 1.3
        else:
            amps = 0.85
        return (amps + self._load_noise()) * volts / 12.0

    def _load_noise(self) -> float:
        """Noise on the input current, the same for one tick: the supply's
        bisection must see a current that rises with the voltage."""
        noise, drawn_at = self._noise
        now = time.monotonic()
        if now - drawn_at >= NOISE_TICK_S:
            noise = self._rng.gauss(0, 0.015)
            self._noise = (noise, now)
        return noise

    def input_ok(self) -> bool:
        return self.input_voltage("vin") >= MIN_INPUT_V

    # -- rails, measured through the fixture -----------------------------------

    def voltage(self, terminal: str) -> float:
        if (self._power_on_at is None or not self.input_ok()
                or (terminal == "1V05" and self.fault == "rail_1v05")):
            return abs(self._rng.gauss(0.02, 0.005))
        return RAILS[terminal] * (1 + self._rng.gauss(0, 0.003))

    # -- serial console ----------------------------------------------------

    def _emit(self, text: str, generation: int) -> None:
        """Write to the console unless the power was cycled since ``generation``."""
        if generation == self._generation:
            self.console.write(text)

    def _boot(self, generation: int, power_on_at: float) -> None:
        if not self.input_ok():
            return  # brown-out: the board never starts
        log = BOOT_LOG[:6] + HANG_LOG if self.fault == "boot_hang" else BOOT_LOG
        for at, line in log:
            delay = power_on_at + at * self.time_scale - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if generation != self._generation:
                return
            self._emit(line + "\n", generation)
        if self.fault == "boot_hang":
            return
        delay = power_on_at + BOOT_DONE_S * self.time_scale - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        with self._lock:
            if generation != self._generation:
                return
            self.state = "login"
        self._emit("\nepc-test login: ", generation)

    def console_line(self, line: str) -> None:
        """Handle one line typed on the console (the console echoes it)."""
        with self._lock:  # a power-off in between must win
            state, generation = self.state, self._generation
            if state == "login" and line == "root":
                self.state = "shell"
        if state == "login":
            reply = "Last login: on ttyS0\n# " if line == "root" else "Login incorrect\n\nepc-test login: "
        elif state == "shell":
            output = self._shell(line.strip())
            reply = (output + "\n" if output else "") + "# "
        else:
            return
        self._emit(line + "\n", generation)
        self._emit(reply, generation)

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

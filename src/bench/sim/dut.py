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

from scpi_sim import Device, Transient

FAULTS = {
    "none": "healthy device",
    "short": "short circuit on the 12 V input",
    "rail_1v05": "1.05 V core rail missing",
    "inrush_spike": "4 A inrush spike of 2 ms (input capacitor too large)",
    "rail_slow": "3.3 V rail ramps up too slowly (soft-start capacitor too large)",
    "boot_hang": "boot hangs in the kernel (SATA reset loop)",
    "sata_slow": "SATA link falls back to 3.0 Gbps (marginal signal path)",
    "can_missing": "CAN controller not detected (no driver message at boot)",
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
VOLTS_PER_AMP = 0.1  # the current probe on vin
SPIKE_S = 0.002      # shorter than the time between two measurements of the supply
RAIL_START_S = 0.05
RAIL_RAMP_S = 0.02
SLOW_RAIL_RAMP_S = 0.2
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
    (2.2, "[    2.201733] mcp251x spi0.0 can0: MCP2515 successfully initialized"),
    (2.3, "[    2.410552] systemd[1]: Reached target multi-user.target"),
]
HANG_LOG = [
    (1.9, "[    1.204511] ata1: COMRESET failed (errno=-16)"),
    (2.6, "[    6.311045] ata1: COMRESET failed (errno=-16)"),
    (3.3, "[   11.418871] ata1: limiting SATA link speed to 3.0 Gbps"),
]
SATA_LINK_UP = "ata1: SATA link up 6.0 Gbps"
SATA_SLOW_LOG = [
    "[    1.204511] ata1: limiting SATA link speed to 3.0 Gbps",
    "[    1.204511] ata1: SATA link up 3.0 Gbps (SStatus 123 SControl 320)",
]


def boot_log(fault: str) -> list[tuple[float, str]]:
    """The console output of a boot with this fault, as (device seconds, line)."""
    if fault == "boot_hang":
        return BOOT_LOG[:7] + HANG_LOG
    if fault == "can_missing":
        return [entry for entry in BOOT_LOG if "can0" not in entry[1]]
    if fault == "sata_slow":
        log = []
        for at, line in BOOT_LOG:
            log += [(at, text) for text in SATA_SLOW_LOG] if SATA_LINK_UP in line else [(at, line)]
        return log
    return BOOT_LOG


class EmbeddedPC(Device):
    terminals = {"vin": "input", "isense": "output", **{rail: "output" for rail in RAILS}}

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
        self._waveforms: dict[str, Transient] = {}  # what a scope sees, after the first power-on

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
                self._waveforms = self._power_on_waveforms(self._power_on_at, vin)
            else:
                self._waveforms = self._power_off_waveforms(time.monotonic())
                self._power_on_at = None
                self.state = "off"
        if powered:
            threading.Thread(target=self._boot, args=(generation, self._power_on_at), daemon=True).start()

    def power_off(self) -> None:
        """Forget the power state (stops a running boot), e.g. when the simulator stops."""
        with self._lock:
            self._generation += 1
            self._waveforms = {}
            self._power_on_at = None
            self.state = "off"

    def _power_on_waveforms(self, now: float, vin: float) -> dict[str, Transient]:
        """The input current and the 3.3 V rail, as they run in ``current()`` and the boot."""
        scale = self.time_scale
        if self.fault == "short":
            amps = [(0, 0), (0, 4.5)]
        elif vin < MIN_INPUT_V:
            amps = [(0, 0), (0, 0.05)]
        else:
            amps = [(0, 0), (0, 2.2)]
            if self.fault == "inrush_spike":
                amps = [(0, 0), (0, 4.0), (SPIKE_S, 4.0), (SPIKE_S, 2.2)]
            amps += [(INRUSH_END_S, 2.2), (INRUSH_END_S, 1.3)]
            if self.fault != "boot_hang":
                amps += [(BOOT_DONE_S, 1.3), (BOOT_DONE_S, 0.85)]
        ramp = SLOW_RAIL_RAMP_S if self.fault == "rail_slow" else RAIL_RAMP_S
        rail = [(0, 0), (RAIL_START_S, 0), (RAIL_START_S + ramp, RAILS["3V3"])]
        if vin < MIN_INPUT_V or self.fault == "short":
            rail = [(0, 0)]
        return {"isense": Transient(now, [(t * scale, a * VOLTS_PER_AMP) for t, a in amps]),
                "3V3": Transient(now, [(t * scale, v) for t, v in rail])}

    def _power_off_waveforms(self, now: float) -> dict[str, Transient]:
        return {"isense": Transient(now, [(0, 0)]), "3V3": Transient(now, [(0, 0)])}

    def waveform(self, terminal: str) -> Transient | None:
        return self._waveforms.get(terminal)

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
        if terminal == "isense":
            return self.current("vin", 12.0) * VOLTS_PER_AMP
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
        for at, line in boot_log(self.fault):
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

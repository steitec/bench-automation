"""The simulated bench: scpi-sim instruments plus the EmbeddedPC as a scpi-sim device.

The instruments are scpi-sim's generic ones, so their answers follow scpi-sim:
`*IDN?` starts with "scpi-sim", numbers come as "+1.25000000E+01", and a value
out of range is SCPI error -222.
"""

import time

import pyvisa
import pytest

from bench.drivers import InstrumentError, PowerSupply
from bench.sim import EmbeddedPC, Simulator


@pytest.fixture
def sim():
    simulator = Simulator(time_scale=0.2, ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0))
    simulator.start()
    yield simulator
    simulator.stop()


def test_scpi_long_and_short_forms(sim):
    psu = sim.bench.psu
    psu.process("SOURce:VOLTage:LEVel 12.5")
    assert psu.process("VOLT?") == "+1.25000000E+01"
    assert psu.process("SYST:ERR?") == '0,"No error"'


def test_unknown_command_queues_scpi_error(sim):
    psu = sim.bench.psu
    assert psu.process("FOO:BAR 1") is None
    assert psu.process("SYST:ERR?") == '-113,"Undefined header"'
    psu.process("VOLT 99")
    assert psu.process("SYST:ERR?") == '-222,"Data out of range"'


def test_multiplexer_channel_lists(sim):
    mux = sim.bench.mux
    mux.process("ROUT:CLOS (@105:107)")  # unused channels: 101-104 carry the rails
    assert mux.process("ROUT:CLOS? (@105,106,108)") == "1,1,0"


def test_closing_two_rails_at_once_is_refused(sim):
    mux = sim.bench.mux
    sim.bench.psu.process("VOLT 12;CURR 3;:OUTP ON")
    mux.process("ROUT:CLOS (@101:102)")  # would short 5V0 and 3V3 together
    assert mux.process("SYST:ERR?") == '-221,"Settings conflict"'


def test_dut_is_a_scpi_sim_device(sim):
    assert sim.dut is sim.bench.devices["dut"]
    assert isinstance(sim.dut, EmbeddedPC)


def test_rail_through_the_fixture(sim):
    sim.bench.psu.process("VOLT 12;CURR 3;:OUTP ON")
    sim.bench.mux.process("ROUT:CLOS (@102)")
    assert float(sim.bench.dmm.process("MEAS:VOLT:DC?")) == pytest.approx(3.3, rel=0.02)


def test_current_limit_pulls_voltage_down():
    simulator = Simulator(fault="short", ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0))
    try:
        psu = simulator.bench.psu
        psu.process("VOLT 12;CURR 3;:OUTP ON")
        assert float(psu.process("MEAS:CURR?")) == pytest.approx(3.0)
        assert float(psu.process("MEAS:VOLT?")) == pytest.approx(8.0, rel=1e-3)
    finally:
        simulator.stop()


def test_inrush_then_boot_current(sim):
    psu = sim.bench.psu
    psu.process("VOLT 12;CURR 3;:OUTP ON")
    assert float(psu.process("MEAS:CURR?")) == pytest.approx(2.2, abs=0.1)
    time.sleep(0.3 * 0.2 + 0.05)
    assert float(psu.process("MEAS:CURR?")) == pytest.approx(1.3, abs=0.1)


def test_driver_reports_instrument_errors(sim):
    resources = sim.resources
    rm = pyvisa.ResourceManager("@py")
    psu = PowerSupply(rm, resources["psu"])
    assert psu.idn().startswith("scpi-sim,PSU")
    with pytest.raises(InstrumentError, match="-222"):
        psu.configure(voltage=50, current_limit=1)
    psu.close()
    rm.close()


def _boots_with_limit(limit):
    simulator = Simulator(time_scale=0.2, ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0))
    try:
        psu = simulator.bench.psu
        psu.process(f"VOLT 12;CURR {limit};:OUTP ON")
        volts = float(psu.process("MEAS:VOLT?"))
        time.sleep(0.7)
        return volts, simulator.dut.state
    finally:
        simulator.stop()


def test_inrush_in_current_limit_sags_like_a_resistive_load():
    # inrush 2.2 A at 12 V: with a 2.0 A limit the supply sags to ~10.9 V, enough to boot
    # (the board's 15 mA current noise moves that point by ~0.075 V: 4 sigma = 0.3 V)
    volts, state = _boots_with_limit(2.0)
    assert volts == pytest.approx(12 * 2.0 / 2.2, abs=0.3)
    assert state == "login"


def test_hard_current_limit_browns_out():
    volts, state = _boots_with_limit(1.0)
    assert volts == pytest.approx(12 * 1.0 / 2.2, abs=0.2)
    assert state != "login"


def test_undervoltage_at_power_on_draws_standby_current():
    simulator = Simulator(time_scale=0.2, ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0))
    try:
        simulator.bench.psu.process("VOLT 10;CURR 3;:OUTP ON")
        assert float(simulator.bench.psu.process("MEAS:CURR?")) == pytest.approx(0.05)
    finally:
        simulator.stop()


def test_stop_leaves_the_device_off():
    simulator = Simulator(time_scale=0.2, ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0))
    simulator.start()
    simulator.bench.psu.process("VOLT 12;CURR 3;:OUTP ON")
    simulator.stop()
    simulator.bench.dmm.process("*OPC?")  # any message after stop runs the device update
    assert simulator.dut.state == "off"


def test_power_off_while_logging_in_leaves_the_device_off():
    dut = EmbeddedPC()
    dut.state = "login"
    after_off = []

    def pull_the_plug(text):  # the echo of the typed line: power goes away right then
        if dut.state == "off":
            after_off.append(text)
        elif text == "root\n":
            dut.power_off()

    dut.console.attach(pull_the_plug)
    dut.console_line("root")
    assert dut.state == "off"
    assert after_off == []


def test_input_current_rises_with_voltage_within_one_measurement():
    # The supply finds its current-limit point by bisection over the board's
    # current; fresh noise on every call made that curve jump, and the search
    # stopped on a wrong step now and then (11.0625 V instead of ~10.9 V).
    board = EmbeddedPC(time_scale=1000.0)  # stays in inrush for the whole test
    board._power_on_at = time.monotonic()
    for _ in range(200):
        amps = [board.current("vin", volts / 10) for volts in range(100, 121)]
        assert amps == sorted(amps)

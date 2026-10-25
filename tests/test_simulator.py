import pyvisa
import pytest

from bench.drivers import InstrumentError, PowerSupply
from bench.sim import EmbeddedPC, Simulator
from bench.sim.server import SimMultiplexer, SimPowerSupply


@pytest.fixture
def dut():
    return EmbeddedPC(time_scale=0.2)


def test_scpi_long_and_short_forms(dut):
    psu = SimPowerSupply(dut)
    psu.handle("SOURce:VOLTage:LEVel 12.5")
    assert psu.handle("VOLT?") == "12.500"
    assert psu.handle("SYST:ERR?") == '0,"No error"'


def test_unknown_command_queues_scpi_error(dut):
    psu = SimPowerSupply(dut)
    assert psu.handle("FOO:BAR 1") is None
    assert psu.handle("SYST:ERR?") == '-113,"Undefined header"'
    psu.handle("VOLT 99")
    assert psu.handle("SYST:ERR?") == '-224,"Illegal parameter value"'


def test_multiplexer_channel_lists(dut):
    mux = SimMultiplexer(dut)
    mux.handle("ROUT:CLOS (@101:103)")
    assert mux.handle("ROUT:CLOS? (@101,102,104)") == "1,1,0"


def test_current_limit_pulls_voltage_down():
    dut = EmbeddedPC(fault="short")
    dut.psu_voltage, dut.psu_current_limit = 12.0, 3.0
    dut.set_output(True)
    volts, amps = dut.output()
    assert amps == 3.0 and volts == pytest.approx(8.0)
    dut.set_output(False)


def test_driver_reports_instrument_errors():
    with Simulator(ports=dict.fromkeys(Simulator.DEFAULT_PORTS, 0)) as resources:
        rm = pyvisa.ResourceManager("@py")
        psu = PowerSupply(rm, resources["psu"])
        assert psu.idn().startswith("BenchSim,PSU")
        with pytest.raises(InstrumentError, match="-224"):
            psu.configure(voltage=50, current_limit=1)
        psu.close()
        rm.close()

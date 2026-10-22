from __future__ import annotations

import pytest

from hil import ScpiInstrument, connect
from hil.errors import SafetyViolationError, TimeoutError_, TransportError
from hil.transports import TcpTransport, create_transport
from hil.transports.tcp import TcpTransport as TT


def test_tcp_roundtrip(fake_instrument, fake_address):
    with TT(fake_address.split("//")[1].split(":")[0], int(fake_address.rsplit(":", 1)[1])) as t:
        assert t.query("*IDN?") == "FAKE,TEST-1000,SN123456,V1.0"
        assert t.query("*OPC?") == "1"


def test_tcp_default_port_used(fake_address):
    addr = fake_address  # uses ephemeral port; just prove create works
    transport = create_transport(__import__("hil").addresses.parse_address(addr))
    assert isinstance(transport, TcpTransport)


def test_tcp_timeout(fake_instrument, fake_address):
    t = TT("127.0.0.1", fake_instrument.port, timeout=0.3)
    t.open()
    try:
        # "OUTP ON" is not a query in the fake -> no response -> timeout
        with pytest.raises(TimeoutError_):
            t.query("OUTP ON", timeout=0.3)
    finally:
        t.close()


def test_tcp_connection_refused():
    t = TT("127.0.0.1", 1, timeout=0.5)  # port 1: nothing there
    with pytest.raises(TransportError) as excinfo:
        t.open()
    assert "Cannot connect" in str(excinfo.value)


def test_instrument_via_connect(fake_instrument, fake_address):
    inst = connect(fake_address)
    try:
        assert inst.idn() == "FAKE,TEST-1000,SN123456,V1.0"
        inst.write("OUTP ON")
        fake_instrument.wait_received("OUTP ON")
    finally:
        inst.transport.close()


def test_identify_parses_fields(fake_instrument, fake_address):
    inst = connect(fake_address)
    try:
        identity = inst.identify()
        assert identity == {
            "manufacturer": "FAKE",
            "model": "TEST-1000",
            "serial": "SN123456",
            "firmware": "V1.0",
        }
    finally:
        inst.transport.close()


def test_drain_errors_empty_queue(fake_instrument, fake_address):
    inst = connect(fake_address)
    try:
        assert inst.drain_errors() == []
    finally:
        inst.transport.close()


def test_safety_policy_via_device_slug(fake_instrument, fake_address):
    inst = connect(fake_address, device_slug="keysight-ac6801b")
    try:
        with pytest.raises(SafetyViolationError):
            inst.write("OUTP ON")  # AC source is query-only by default
        inst.query("*IDN?")  # allowed by the allow-list
    finally:
        inst.transport.close()


def test_write_before_open_fails():
    t = TT("127.0.0.1", 5025)
    with pytest.raises(TransportError):
        t.write("*IDN?")

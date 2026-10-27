"""Tests for hil.discovery — live bench enumeration without any hardware.

Every backend is optional and every probe best-effort, so the tests drive the
fan-out with monkeypatched candidate/identify helpers and assert the *contract*:
a missing library becomes a warning with an install hint (never an exception), a
found resource becomes a uniform record with an address an agent can feed straight
back into hil_exec, and a reference match is only reported when confident.
"""

from __future__ import annotations

import pytest

from hil import discovery, reference

_NO_BACKENDS = {"visa": False, "serial": False, "zeroconf": False, "debug": False}


def test_transport_classification():
    assert discovery._transport_for_resource("USB0::6833::42152::X::0::INSTR") == "usb-tmc"
    assert discovery._transport_for_resource("GPIB0::24::INSTR") == "gpib"
    assert discovery._transport_for_resource("TCPIP0::1.2.3.4::inst0::INSTR") == "lan"
    assert discovery._transport_for_resource("ASRL1::INSTR") == "serial"
    assert discovery._address_for_resource("USB0::1::INSTR") == "visa://USB0::1::INSTR"


def test_missing_usb_serial_backends_become_warnings(monkeypatch):
    monkeypatch.setattr(discovery, "_backend_flags", lambda: dict(_NO_BACKENDS))
    res = discovery.discover(("usb", "serial"), identify=False)
    assert res["ok"] is True
    assert res["devices"] == []
    joined = " ".join(res["warnings"]).lower()
    assert "usb" in joined and "serial" in joined
    assert "install" in joined          # every warning carries a concrete install hint


def test_visa_candidate_becomes_uniform_record(monkeypatch):
    monkeypatch.setattr(discovery, "_backend_flags",
                        lambda: {**_NO_BACKENDS, "visa": True})
    monkeypatch.setattr(discovery, "_visa_candidates",
                        lambda: ["USB0::6833::42152::DP9D252300435::0::INSTR"])
    monkeypatch.setattr(discovery, "_identify", lambda addr, timeout: {
        "idn": "Rigol Technologies,DP932E,DP9D252300435,00.00.01.00.19",
        "identity": {"manufacturer": "Rigol Technologies", "model": "DP932E",
                     "serial": "DP9D252300435", "firmware": "00.00.01.00.19"},
    })
    monkeypatch.setattr(discovery, "_match_reference", lambda idn: ("rigol-dp932e", "Rigol DP932E"))

    res = discovery.discover(("usb",), identify=True)
    assert res["count"] == 1
    dev = res["devices"][0]
    assert dev["transport"] == "usb-tmc"
    assert dev["backend"] == "visa"
    assert dev["address"].startswith("visa://USB0::")
    assert dev["idn"].startswith("Rigol")
    assert dev["matched_slug"] == "rigol-dp932e"
    assert dev["identity"]["model"] == "DP932E"


def test_identify_failure_degrades_not_raises(monkeypatch):
    monkeypatch.setattr(discovery, "_backend_flags", lambda: {**_NO_BACKENDS, "visa": True})
    monkeypatch.setattr(discovery, "_visa_candidates", lambda: ["USB0::1::INSTR"])
    monkeypatch.setattr(discovery, "_identify", lambda addr, timeout: None)  # refuses *IDN?
    res = discovery.discover(("usb",), identify=True)
    assert res["ok"] is True and res["count"] == 1
    dev = res["devices"][0]
    assert "idn" not in dev and "matched_slug" not in dev   # listed, just not identified


def test_match_reference_confident_and_rejects_false_positive():
    idx = reference.default_index()
    if not idx.available():
        pytest.skip("reference catalog not present")
    slug, _name = discovery._match_reference(
        "Rigol Technologies,DP932E,DP9D252300435,00.00.01.00.19")
    assert slug == "rigol-dp932e"
    # a vendor-only overlap must be rejected, never mislabeled onto live hardware
    assert discovery._match_reference("Keysight Technologies,34461A,MY5301,1.0") == ("", "")
    assert discovery._match_reference("") == ("", "")


def test_backend_hint_is_concrete():
    hint = discovery._backend_hint("usb")
    assert "install" in hint.lower()

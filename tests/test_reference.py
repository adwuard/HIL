"""Tests for hil.reference — SCPI mnemonic matching, catalog confidence, help_lookup.

Two bugs from the field are pinned here:

* a *vendor-only* catalog overlap (asking for a "Keysight 34461A" and hitting a
  different Keysight entry) must NOT be trusted — returning the wrong device's
  SCPI is worse than reporting no match;
* a mid-mnemonic fragment ("VOLT" inside ``:MEASure[:SCALar][:VOLTage][:DC]?``)
  must still match — the old prefix-anchored matcher could not reach it.
"""

from __future__ import annotations

import json

import pytest

from hil import reference
from hil.reference import ReferenceIndex, _confident, _scpi_match

CATALOG = [
    {"slug": "rigol-dp932e", "name": "Rigol DP932E", "category": "power-supply", "url": "u"},
    {"slug": "keysight-ac6801b", "name": "Keysight AC6801B", "category": "ac-power-source", "url": "u"},
    {"slug": "keithley-2400", "name": "Keithley 2400", "category": "smu", "url": "u"},
]


def _index_over(tmp_path, entries=CATALOG) -> ReferenceIndex:
    (tmp_path / "catalog.json").write_text(json.dumps({"devices": entries}), encoding="utf-8")
    return ReferenceIndex(roots=[tmp_path])


# -- SCPI mnemonic matching --------------------------------------------------
@pytest.mark.parametrize("mnemonic,query", [
    (":OUTPut[:STATe]", "OUTP"),
    (":OUTPut[:STATe]", "OUTPUT"),
    (":OUTPut[:STATe]", "OUTP:STAT"),
    (":OUTPut[:STATe]", "OUTPUT:STATE"),
    (":MEASure[:SCALar][:VOLTage][:DC]?", "MEAS:VOLT"),
    (":MEASure[:SCALar][:VOLTage][:DC]?", "VOLT"),   # mid-mnemonic fragment
    (":INSTrument:NSELect", "INST:NSEL"),
    (":SOURce[:VOLTage]", "SOUR:VOLT"),
])
def test_scpi_match_positive(mnemonic, query):
    assert _scpi_match(mnemonic, query) is True


@pytest.mark.parametrize("mnemonic,query", [
    (":OUTPut[:STATe]", "CURR"),
    (":MEASure:CURRent?", "VOLT"),
    (":SOURce:VOLTage", "MEAS"),
    ("", "OUTP"),
    (":OUTPut", ""),
])
def test_scpi_match_negative(mnemonic, query):
    assert _scpi_match(mnemonic, query) is False


# -- confidence gate ---------------------------------------------------------
def test_confident_accepts_exact_or_high_score():
    assert _confident({"exact": True, "score": 0.45}) is True
    assert _confident({"score": 1.0}) is True
    assert _confident({"score": reference.MIN_CONFIDENCE}) is True


def test_confident_rejects_low_or_missing_or_bad():
    assert _confident({"score": 0.45}) is False
    assert _confident({"score": 0.0}) is False
    assert _confident(None) is False
    assert _confident({}) is False
    assert _confident({"score": "bogus"}) is False


# -- catalog matching --------------------------------------------------------
def test_match_exact_model_is_confident(tmp_path):
    idx = _index_over(tmp_path)
    best = idx.best_match("DP932E")
    assert best["slug"] == "rigol-dp932e"
    assert _confident(best) is True


def test_match_rejects_vendor_only_false_positive(tmp_path):
    # "Keysight 34461A" is not in the catalog; it must not confidently resolve to
    # a *different* Keysight model just because the vendor token overlaps.
    idx = _index_over(tmp_path)
    best = idx.best_match("Keysight 34461A")
    assert not (best and _confident(best))


def test_match_full_idn_string(tmp_path):
    idx = _index_over(tmp_path)
    best = idx.best_match("Rigol Technologies,DP932E,DP9D252300435,00.00.01")
    assert best["slug"] == "rigol-dp932e" and _confident(best)


# -- help_lookup -------------------------------------------------------------
def test_help_lookup_rejects_low_confidence_with_near_misses(tmp_path, monkeypatch):
    idx = _index_over(tmp_path)
    monkeypatch.setattr(reference, "default_index", lambda: idx)
    res = reference.help_lookup("Keysight 34461A")
    assert res["ok"] is False
    assert res["matched"] is None
    assert res.get("near")                      # near-misses surfaced, not a match
    assert "NOT confirmed" in res["hint"]


def test_help_lookup_confident_match_without_commands_hints_scrape(tmp_path, monkeypatch):
    idx = _index_over(tmp_path)
    monkeypatch.setattr(reference, "default_index", lambda: idx)
    res = reference.help_lookup("DP932E")
    assert res["ok"] is True
    assert res["matched"]["slug"] == "rigol-dp932e"
    assert res["commands"] == []                # no command corpus on this synthetic root
    assert "scrape" in res["hint"]


def test_help_lookup_empty_query_is_reportable(tmp_path, monkeypatch):
    idx = _index_over(tmp_path)
    monkeypatch.setattr(reference, "default_index", lambda: idx)
    res = reference.help_lookup("")
    assert res["ok"] is False and "hint" in res


def test_help_lookup_real_corpus_dp932e_ground_truth():
    # The exact command the failed field session guessed wrong: DP932E output-enable.
    idx = reference.default_index()
    if not (idx.available() and idx.commands_available()):
        pytest.skip("reference command corpus not present on this host")
    res = reference.help_lookup("Rigol DP932E", command="OUTP:STAT")
    assert res["ok"] is True
    assert res["matched"]["slug"] == "rigol-dp932e"
    mnemonics = [c["mnemonic"] for c in res["commands"]]
    assert any("OUTPut" in m for m in mnemonics)

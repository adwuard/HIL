"""Tests for the logic-analyzer driver, its backends, and capture artifacts.

The daemon backend is exercised against a fake in-thread JSON-RPC server, and the
simulator backend against a temp spool — so everything runs with no hardware and
no ``atk-logic`` daemon installed, exactly like the PPK2 driver tests.
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import replace

import pytest

from hil.artifacts import ArtifactSpool, CaptureArtifact, metadata_path, new_id
from hil.drivers import available_drivers, get_driver
from hil.drivers.logic import (
    LogicDaemonBackend,
    LogicInstrument,
    LogicSimBackend,
    logic_driver,
)
from hil.errors import HilError, SafetyViolationError
from hil.addresses import Address
from hil.registry import load_registry
from hil.registry.schema import SafetyPolicy


# -- artifacts --------------------------------------------------------------
def test_capture_artifact_roundtrip():
    art = CaptureArtifact(id="a1", kind="logic", path="/tmp/a1.csv", format="csv",
                          channels=[0, 1], sample_rate_hz=1e6, sample_count=42,
                          duration_s=0.5, size_bytes=1234, trigger={"channel": 0})
    d = art.to_dict()
    assert d["bytes"] == 1234 and d["kind"] == "logic" and d["channels"] == [0, 1]
    back = CaptureArtifact.from_dict(d)
    assert back.id == "a1" and back.size_bytes == 1234 and back.sample_count == 42
    assert back.trigger == {"channel": 0}


def test_spool_record_resolve_retain_cleanup(tmp_path):
    spool = ArtifactSpool(tmp_path / "spool")
    p = spool.new_path("logic", "csv")
    assert p.suffix == ".csv" and p.parent == spool.root
    p.write_text("index,time_s,ch0\n0,0,1\n", encoding="utf-8")
    art = CaptureArtifact(id="cap-x", kind="logic", path=str(p), format="csv")
    spool.record(art)
    assert metadata_path(p).exists()
    assert art.size_bytes == p.stat().st_size          # record() refreshed the size
    assert spool.read_metadata(p).id == "cap-x"
    assert spool.resolve("cap-x") == p                  # resolve by id
    kept = spool.retain("cap-x", tmp_path / "keep")     # retain into a directory
    assert kept.exists() and kept.parent == tmp_path / "keep"
    assert spool.cleanup(max_age_s=-1)                  # everything is "older than -1s"
    assert not p.exists() and not metadata_path(p).exists()


def test_new_id_is_unique_and_prefixed():
    a, b = new_id("logic"), new_id("logic")
    assert a != b and a.startswith("logic-")


# -- simulator backend ------------------------------------------------------
def test_sim_backend_capture_and_decode_files(tmp_path):
    spool = ArtifactSpool(tmp_path)
    be = LogicSimBackend(spool=spool)
    info = be.hello()
    assert info["device"]["model"] == "ATK-Logic" and "i2c" in info["decoders"]
    be.configure({"channels": [0, 1], "sample_rate_hz": 1e6, "duration_s": 0.25})
    cid = be.arm()
    assert be.status(cid)["state"] == "done"
    art = be.fetch(cid, fmt="csv")
    p = art["path"]
    assert p.endswith(".csv") and art["sample_count"] == 250000
    header = open(p, encoding="utf-8").readline().strip()
    assert header.startswith("index,time_s,ch0,ch1")
    dec = be.decode(p, [{"name": "i2c"}], fmt="csv")
    assert dec["kind"] == "decoded" and dec["frames"] > 0


def test_sim_backend_rows_are_capped(tmp_path):
    # A nominal huge capture still writes a small, bounded file.
    be = LogicSimBackend(spool=ArtifactSpool(tmp_path), max_rows=64)
    be.configure({"sample_rate_hz": 1e6, "duration_s": 10})   # 10,000,000 samples
    be.arm()
    art = be.fetch(fmt="csv")
    assert art["sample_count"] == 10_000_000
    lines = sum(1 for _ in open(art["path"], encoding="utf-8"))
    assert lines == 65                                        # 64 rows + header


# -- instrument facade (on the simulator) -----------------------------------
def _instrument(tmp_path, device=None):
    return LogicInstrument(LogicSimBackend(spool=ArtifactSpool(tmp_path)),
                           device=device, timeout=5.0)


def test_instrument_facade_flow(tmp_path):
    inst = _instrument(tmp_path)
    assert inst.idn().startswith("ALIENTEK,ATK-Logic,")
    inst.write(":CONF:CHAN 0,1")
    inst.write(":CONF:RATE 4000000")
    inst.write(":CONF:FORM csv")
    inst.write(":TRIG:CHAN 0")
    inst.write(":TRIG:EDGE R")
    inst.write(":ACQ:ARM")
    assert inst.query(":ACQ:STAT?") == "done"
    path = inst.query(":FETCh?")
    assert path.endswith(".csv") and path
    dec = inst.query(":DEC i2c?")
    assert dec.endswith(".csv")
    assert inst.query("*OPC?") == "1"
    assert inst.opc() is True


def test_instrument_run_capture_and_decode(tmp_path):
    inst = _instrument(tmp_path)
    wave = inst.run_capture(channels=[0, 1], sample_rate_hz=1e6, duration_s=0.1, fmt="csv")
    assert isinstance(wave, CaptureArtifact) and wave.kind == "logic"
    assert wave.sample_count == 100000 and wave.path.endswith(".csv")
    decoded = inst.decode("i2c")
    assert decoded.kind == "decoded" and decoded.parent == wave.id
    assert decoded.frames > 0 and decoded.decoders == ["i2c"]


def test_instrument_capture_is_bounded(tmp_path):
    inst = _instrument(tmp_path)
    samples = inst.capture("0", seconds=0.01, units="logic")
    assert 0 < len(samples) <= 512
    assert all(s.units == "logic" and s.value in (0.0, 1.0) for s in samples)


def test_instrument_decode_without_capture_errors(tmp_path):
    inst = _instrument(tmp_path)
    with pytest.raises(HilError):
        inst.decode("i2c")


def test_instrument_unsupported_verbs_raise(tmp_path):
    inst = _instrument(tmp_path)
    with pytest.raises(HilError):
        inst.write(":BOGUS 1")
    with pytest.raises(HilError):
        inst.query(":BOGUS?")


def test_instrument_safety_policy_is_enforced(tmp_path):
    dev = load_registry()["alientek-atk-logic"]
    blocked = replace(dev, safety=replace(dev.safety, blocked=(":CONF:RATE",)))
    inst = _instrument(tmp_path, device=blocked)
    with pytest.raises(SafetyViolationError):
        inst.write(":CONF:RATE 1000000")


# -- driver registry + factory ---------------------------------------------
def test_logic_driver_is_registered():
    assert "logic" in available_drivers()
    assert callable(get_driver("logic"))


def test_logic_driver_factory_needs_host():
    with pytest.raises(HilError):
        logic_driver(Address(scheme="logic", host="", port=5555), None, 5.0)


# -- daemon backend over a fake JSON-RPC server ----------------------------
class FakeLogicDaemon:
    """A minimal newline-delimited JSON-RPC 2.0 server for the daemon client."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.methods: list[str] = []
        self._client = None
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return                      # listening socket closed during teardown
        self._client = conn
        buf = b""
        while self._running:
            try:
                conn.settimeout(0.2)
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                self._handle(conn, line.decode().strip())

    def _handle(self, conn, line):
        if not line:
            return
        req = json.loads(line)
        method = req.get("method")
        self.methods.append(method)
        rid = req.get("id")
        if method == "boom":
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32003, "message": "capture failed"}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid, "result": self._result(method, req)}
        conn.sendall((json.dumps(resp) + "\n").encode())

    def _result(self, method, req):
        if method == "hello":
            return {"protocol": "1", "device": {"vendor": "ALIENTEK", "model": "ATK-Logic",
                    "serial": "S1", "variant": "FPGA"}, "channels": 16, "decoders": ["i2c"]}
        if method == "configure":
            return {"ok": True, "config": req.get("params", {})}
        if method == "arm":
            return {"capture_id": "cid-1"}
        if method in ("status", "wait"):
            return {"state": "done", "samples_captured": 1000}
        if method == "fetch":
            return {"artifact": {"id": "a1", "kind": "logic", "path": "/spool/a1.srzip",
                                 "format": "srzip", "sample_count": 1000}}
        if method == "decode":
            return {"artifact": {"id": "d1", "kind": "decoded", "path": "/spool/d1.csv",
                                 "format": "csv", "frames": 7}}
        if method == "ping":
            return {"pong": True}
        return {"ok": True}

    def close(self):
        self._running = False
        for s in (self._client, self._sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


@pytest.fixture
def fake_daemon():
    d = FakeLogicDaemon()
    yield d
    d.close()


def test_daemon_backend_rpc_roundtrip(fake_daemon):
    be = LogicDaemonBackend("127.0.0.1", fake_daemon.port, timeout=3.0)
    be.open()
    try:
        assert be.hello()["device"]["variant"] == "FPGA"
        assert be.configure({"channels": 8})["ok"] is True
        assert be.arm() == "cid-1"
        assert be.wait("cid-1", 1.0)["state"] == "done"
        art = be.fetch("cid-1", fmt="srzip")
        assert art["path"] == "/spool/a1.srzip"
        dec = be.decode("/spool/a1.srzip", [{"name": "i2c"}])
        assert dec["frames"] == 7
        assert "hello" in fake_daemon.methods and "fetch" in fake_daemon.methods
    finally:
        be.close()


def test_daemon_backend_surfaces_rpc_errors(fake_daemon):
    be = LogicDaemonBackend("127.0.0.1", fake_daemon.port, timeout=3.0)
    be.open()
    try:
        with pytest.raises(HilError) as ei:
            be._call("boom")
        assert "capture failed" in str(ei.value)
    finally:
        be.close()


def test_daemon_backend_info(fake_daemon):
    be = LogicDaemonBackend("127.0.0.1", fake_daemon.port, timeout=3.0)
    assert "atk-logic-daemon@127.0.0.1" in be.info()

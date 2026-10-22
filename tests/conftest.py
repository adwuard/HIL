"""Shared test fixture: a fake SCPI instrument over a local TCP socket."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def index_html() -> str:
    """Offline /equipment index fixture (Framer handover)."""
    return (FIXTURES / "reference_index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def device_html() -> str:
    """Offline instrument-page fixture (Framer handover + ld+json)."""
    return (FIXTURES / "reference_device.html").read_text(encoding="utf-8")

RESPONSES = {
    "*IDN?": "FAKE,TEST-1000,SN123456,V1.0",
    "*OPC?": "1",
    "SYST:ERR?": '0,"NO ERROR"',
    "MEAS:VOLT?": "3.14159",
}


class FakeInstrument:
    """Minimal SCPI-over-raw-socket server for tests."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._client: socket.socket | None = None
        self.received: list[str] = []
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._sock.settimeout(0.5)
            while self._running:
                try:
                    self._client, _ = self._sock.accept()
                except socket.timeout:
                    continue
                self._client.settimeout(0.5)
                self._serve_client()
        except OSError:
            pass

    def _serve_client(self) -> None:
        buffer = b""
        while self._running:
            try:
                chunk = self._client.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                self._handle(line.decode().strip())

    def _handle(self, command: str) -> None:
        self.received.append(command)
        if command in RESPONSES:
            self._client.sendall((RESPONSES[command] + "\n").encode())
        elif command.endswith("?"):
            self._client.sendall(b"?\n")  # generic echo reply for unknown queries

    def close(self) -> None:
        self._running = False
        for sock in (self._client, self._sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def wait_received(self, command: str, timeout: float = 2.0) -> None:
        """Block until the server thread has handled `command` (avoid races)."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if command in self.received:
                return
            time.sleep(0.01)
        raise AssertionError(f"server never received '{command}'; got {self.received}")


@pytest.fixture
def fake_instrument():
    server = FakeInstrument()
    yield server
    server.close()


@pytest.fixture
def fake_address(fake_instrument: FakeInstrument) -> str:
    return f"tcp://127.0.0.1:{fake_instrument.port}"

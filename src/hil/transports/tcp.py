"""Raw-socket SCPI transport (LXI / VXI-11-free 'telnet raw' on port 5025).

This is the standard way modern instruments speak SCPI over LAN: a plain TCP
socket where commands are terminated by '\\n' and responses likewise. It needs
no dependencies — pure stdlib — so it is always available.
"""

from __future__ import annotations

import socket

from ..errors import TimeoutError_, TransportError
from .base import BaseTransport

READ_CHUNK = 4096


class TcpTransport(BaseTransport):
    """Plain TCP socket transport, default port 5025 (SCPI raw socket)."""

    def __init__(self, host: str, port: int = 5025, timeout: float = 5.0) -> None:
        super().__init__(timeout=timeout)
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._buffer = bytearray()

    # -- lifecycle --------------------------------------------------------

    def open(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise TransportError(
                f"Cannot connect to {self.host}:{self.port}: {exc}. "
                f"Check the instrument IP, that SCPI-over-LAN is enabled, "
                f"and port {self.port} is open."
            ) from exc
        self._sock = sock
        self._opened = True

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._opened = False

    # -- io ----------------------------------------------------------------

    def write(self, data: str) -> None:
        if self._sock is None:
            raise TransportError("Transport is not open.")
        try:
            self._sock.sendall((data + "\n").encode("ascii", errors="replace"))
        except OSError as exc:
            raise self._wrap("write", exc) from exc  # type: ignore[arg-type]

    def read(self, timeout: float | None = None) -> str:
        if self._sock is None:
            raise TransportError("Transport is not open.")
        timeout = timeout if timeout is not None else self.timeout
        # Return a buffered line if a previous read pulled in more than one.
        if b"\n" in self._buffer:
            return self._pop_line()
        try:
            self._sock.settimeout(timeout)
            while True:
                chunk = self._sock.recv(READ_CHUNK)
                if not chunk:
                    raise TransportError(
                        "Connection closed by instrument while reading."
                    )
                self._buffer.extend(chunk)
                if b"\n" in self._buffer:
                    return self._pop_line()
        except socket.timeout as exc:
            raise TimeoutError_(
                f"No response from {self.host}:{self.port} within {timeout}s."
            ) from exc
        except OSError as exc:
            raise self._wrap("read", exc) from exc  # type: ignore[arg-type]

    # -- internals -----------------------------------------------------------

    def _pop_line(self) -> str:
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = bytearray(rest)
        return line.rstrip(b"\r").decode("utf-8", errors="replace")

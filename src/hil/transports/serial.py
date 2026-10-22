"""RS-232 serial transport (lazy pyserial backend).

Requires the 'serial' extra:  pip install 'hil-harness[serial]'
"""

from __future__ import annotations

from ..errors import TransportError, TransportNotFoundError
from .base import BaseTransport

try:  # pragma: no cover - import guard
    import serial  # type: ignore[import-not-found]

    _HAS_SERIAL = True
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]
    _HAS_SERIAL = False


class SerialTransport(BaseTransport):
    """RS-232 transport over pyserial. Baud defaults to 9600."""

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        timeout: float = 5.0,
        **extra: object,
    ) -> None:
        super().__init__(timeout=timeout)
        self.port = port
        self.baud = baud
        self.extra = extra
        self._ser = None

    def open(self) -> None:
        if self._ser is not None:
            return
        if not _HAS_SERIAL:
            raise TransportNotFoundError("serial", "serial", "pyserial")
        try:
            self._ser = serial.Serial(  # type: ignore[union-attr]
                port=self.port,
                baudrate=self.baud,
                timeout=self.timeout,
                **self.extra,  # type: ignore[arg-type]
            )
        except Exception as exc:
            raise TransportError(
                f"Cannot open serial port {self.port} @ {self.baud} baud: {exc}. "
                f"On macOS the port is usually /dev/tty.usbserial-*; "
                f"check it exists and is not in use."
            ) from exc
        self._opened = True

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self._opened = False

    def write(self, data: str) -> None:
        if self._ser is None:
            raise TransportError("Transport is not open.")
        try:
            self._ser.write((data + "\r\n").encode("ascii", errors="replace"))
            self._ser.flush()
        except Exception as exc:
            raise self._wrap("write", exc) from exc  # type: ignore[arg-type]

    def read(self, timeout: float | None = None) -> str:
        if self._ser is None:
            raise TransportError("Transport is not open.")
        try:
            raw = self._ser.readline()  # honours per-open timeout
            return raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
        except Exception as exc:
            raise self._wrap("read", exc) from exc  # type: ignore[arg-type]

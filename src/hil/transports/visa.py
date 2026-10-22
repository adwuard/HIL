"""VISA transport (lazy PyVISA backend) — GPIB, USB-TMC, TCPIP INSTR, etc.

Requires the 'visa' extra:  pip install 'hil-harness[visa]'

VISA covers everything the raw transports cannot: GPIB cards, USB-TMC
instruments, and TCPIP::...::INSTR (VXI-11) resources. The resource string is
passed to PyVISA verbatim, e.g. 'GPIB::24::INSTR' or
'TCPIP::192.168.1.5::hislip0::INSTR'.
"""

from __future__ import annotations

from ..errors import TransportError, TransportNotFoundError
from .base import BaseTransport

try:  # pragma: no cover - import guard
    import pyvisa  # type: ignore[import-not-found]

    _HAS_VISA = True
except ImportError:  # pragma: no cover
    pyvisa = None  # type: ignore[assignment]
    _HAS_VISA = False


class VisaTransport(BaseTransport):
    """PyVISA-backed transport. Resource string used verbatim."""

    def __init__(self, resource: str, timeout: float = 5.0) -> None:
        super().__init__(timeout=timeout)
        self.resource = resource
        self._rm = None
        self._inst = None

    def open(self) -> None:
        if self._inst is not None:
            return
        if not _HAS_VISA:
            raise TransportNotFoundError("visa", "visa", "PyVISA")
        try:
            self._rm = pyvisa.ResourceManager()  # type: ignore[union-attr]
            self._inst = self._rm.open_resource(
                self.resource,
                timeout=int(self.timeout * 1000),
                read_termination="\n",
                write_termination="\n",
            )
        except Exception as exc:
            raise TransportError(
                f"VISA could not open resource '{self.resource}': {exc}. "
                f"Note: TCPIP/GPIB/USB resources need a VISA backend "
                f"(e.g. 'pip install pyvisa-py' for pure-Python, or NI-VISA/Keysight IO Libraries)."
            ) from exc
        self._opened = True

    def close(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
            self._inst = None
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None
        self._opened = False

    def write(self, data: str) -> None:
        if self._inst is None:
            raise TransportError("Transport is not open.")
        try:
            self._inst.write(data)
        except Exception as exc:
            raise self._wrap("write", exc) from exc  # type: ignore[arg-type]

    def read(self, timeout: float | None = None) -> str:
        if self._inst is None:
            raise TransportError("Transport is not open.")
        timeout = timeout if timeout is not None else self.timeout
        try:
            if timeout != self.timeout:
                self._inst.timeout = int(timeout * 1000)
            return self._inst.read().rstrip("\r\n")
        except Exception as exc:
            raise self._wrap("read", exc) from exc  # type: ignore[arg-type]

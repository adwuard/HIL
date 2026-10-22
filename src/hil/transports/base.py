"""Transport abstraction: a uniform byte-pipe to an instrument.

Backends (TCP raw socket, serial, VISA) all implement this protocol, so the
SCPI layer above never needs to know how bytes physically travel.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..errors import TimeoutError_, TransportError


@runtime_checkable
class Transport(Protocol):
    """A byte-pipe to an instrument (SCPI raw socket, serial, or VISA)."""

    def open(self) -> None:
        """Open the connection. Raises TransportError if it fails."""
        ...

    def close(self) -> None:
        """Close the connection. Never raises."""
        ...

    def write(self, data: str) -> None:
        """Send a command (no newline handling — caller appends terminators)."""
        ...

    def read(self, timeout: float | None = None) -> str:
        """Read until the instrument's message terminator (usually '\\n')."""
        ...

    def query(self, command: str, timeout: float | None = None) -> str:
        """Write then read. Most transports implement this as write+read."""
        ...


class BaseTransport:
    """Shared plumbing for transports (context manager, default query)."""

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self._opened = False

    def __enter__(self) -> "BaseTransport":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def query(self, command: str, timeout: float | None = None) -> str:
        self.write(command)
        return self.read(timeout=timeout if timeout is not None else self.timeout)

    # helpers for subclasses ---------------------------------------------

    def _wrap(self, op: str, exc: Exception) -> Exception:
        """Re-raise library exceptions as TransportError with context."""
        if isinstance(exc, TimeoutError):
            return TimeoutError_(
                f"{type(self).__name__}: {op} timed out. "
                f"Increase --timeout or check the instrument is on-line."
            )
        return TransportError(f"{type(self).__name__}: {op} failed: {exc}")

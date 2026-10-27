"""In-memory SCPI instrument simulator for hardware-free bench runs.

A :class:`SimTransport` implements the same transport protocol as the TCP /
serial / VISA backends, so it drops straight under
:class:`hil.scpi.ScpiInstrument` — including the device safety policy, which is
enforced above the transport. That lets an agent (or CI) *dry-run* a procedure,
see the exact command sequence, and check assertions against canned replies
before touching a real instrument.

Replies resolve in this order:

1. IEEE-488.2 common commands (``*IDN?``, ``*OPC?``, ``*RST``) and the error
   queue (``SYSTem:ERRor?``) are handled natively.
2. An explicit ``responses`` map (exact, then case-insensitive match).
3. ``default_query_reply`` for any other ``...?`` query.

Every write/query is recorded (``writes`` / ``queries``) so a run report can
show what the procedure actually sent.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from ..transports.base import BaseTransport

DEFAULT_IDN = "SIM,INSTRUMENT,SN0001,1.0"
NO_ERROR = '0,"NO ERROR"'


class SimTransport(BaseTransport):
    """A fake instrument that replies from a static response map."""

    #: Marks this transport as hardware-free so results can carry a loud
    #: ``simulated: true`` flag and never be mistaken for a real bench reading.
    simulated = True

    def __init__(
        self,
        responses: Mapping[str, str] | None = None,
        *,
        idn: str = DEFAULT_IDN,
        default_query_reply: str = "0",
        error_queue: Iterable[str] = (),
        timeout: float = 5.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.responses: dict[str, str] = dict(responses or {})
        self.idn = idn
        self.default_query_reply = default_query_reply
        self._errors: list[str] = list(error_queue)
        self.writes: list[str] = []
        self.queries: list[str] = []
        self._staged: str | None = None

    # -- transport protocol -------------------------------------------------

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def write(self, data: str) -> None:
        command = data.strip()
        self.writes.append(command)
        if command.endswith("?"):
            self.queries.append(command)
            reply = self._reply_for(command)
            self._staged = self.default_query_reply if reply is None else reply
        else:
            self._staged = None

    def read(self, timeout: float | None = None) -> str:
        out = self._staged if self._staged is not None else ""
        self._staged = None
        return out

    # -- simulation ---------------------------------------------------------

    def push_error(self, entry: str) -> None:
        """Queue a ``SYSTem:ERRor?`` entry, e.g. ``'-222,"Data out of range"'``."""
        self._errors.append(entry)

    def _reply_for(self, command: str) -> str | None:
        cmd = command.strip()
        head = cmd.rstrip("?").upper()
        if cmd.upper() == "*IDN?":
            return self.idn
        if cmd.upper() == "*OPC?":
            return "1"
        if head in ("SYST:ERR", "SYSTEM:ERR", "SYSTEM:ERROR", "SYST:ERROR"):
            return self._errors.pop(0) if self._errors else NO_ERROR
        if cmd in self.responses:
            return self.responses[cmd]
        upper = cmd.upper()
        for key, value in self.responses.items():
            if key.strip().upper() == upper:
                return value
        # A setting command (no '?') has no reply.
        return None if not cmd.endswith("?") else self.default_query_reply

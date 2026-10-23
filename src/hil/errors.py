"""Exception hierarchy for the HIL harness.

Every error carries an agent-actionable message: what failed, why, and what
to do next (e.g. which extra to install or which address syntax to use).
"""

from __future__ import annotations


class HilError(Exception):
    """Base class for all HIL harness errors."""


class TransportError(HilError):
    """A transport failed to open, write, read, or close."""


class TransportNotFoundError(TransportError):
    """The requested transport backend is not installed."""

    def __init__(self, scheme: str, extra: str, package: str) -> None:
        super().__init__(
            f"Transport '{scheme}://' requires the '{package}' package, "
            f"which is not installed. Install it with: pip install 'hil-harness[{extra}]' "
            f"(or 'hil-harness[all]' for every backend)."
        )
        self.scheme = scheme
        self.extra = extra
        self.package = package


class AddressError(HilError):
    """A connection address could not be parsed or resolved."""


class DeviceNotFoundError(HilError):
    """A device slug is not present in the registry."""


class RegistryError(HilError):
    """A device definition file is invalid."""


class SafetyViolationError(HilError):
    """A command was blocked by a device safety policy."""


class InstrumentError(HilError):
    """The instrument reported an error (SCPI error queue or timeout)."""


class TimeoutError_(InstrumentError):
    """The instrument did not respond within the timeout."""


class BenchError(HilError):
    """A test-bench procedure failed to load, validate, or run."""


class ProvisionError(HilError):
    """A third-party backend could not be checked, downloaded, or built."""


class ProcedureError(BenchError):
    """A bench procedure spec is malformed or invalid (bad step, missing arg)."""

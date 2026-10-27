"""Live bench discovery — enumerate the hardware actually attached to THIS host.

``hil_devices`` lists the *registry catalogue* (a static set of curated profiles);
it has never been able to answer "what is plugged in right now?". That gap is what
forces an agent to fall back to ``ioreg`` / ``lsusb`` / ``arp`` / ``nc`` and inline
``pyvisa`` scripts. This module is the single call that replaces all of that.

:func:`discover` fans out over the interfaces whose backends are importable and
returns one uniform record per candidate::

    {"address": "visa://USB0::6833::42152::DP9D252300435::0::INSTR",
     "transport": "usb-tmc", "backend": "visa",
     "resource": "USB0::6833::42152::DP9D252300435::0::INSTR",
     "idn": "Rigol Technologies,DP932E,DP9D252300435,00.00.01.00.19",
     "identity": {"manufacturer": "Rigol", "model": "DP932E", ...},
     "matched_slug": "rigol-dp932e", "matched_name": "Rigol DP932E"}

Every backend is optional and every probe is best-effort: a missing library or an
instrument that refuses ``*IDN?`` degrades to a ``warnings`` entry, never an
exception. ``identify=False`` skips the (slower) ``*IDN?`` round-trip and just
lists what the OS/VISA can see.
"""

from __future__ import annotations

from typing import Any, Callable

DEFAULT_INTERFACES = ("usb", "serial", "lan", "debug")


def _backend_hint(backend: str) -> str:
    """Concrete install hint for a missing discovery backend (never a placeholder)."""
    try:
        from .pyinstall import backend_hint

        return backend_hint(backend)
    except Exception:  # noqa: BLE001 - a hint failure must not break discovery
        return f"Install it with: hil setup {backend}"


def _backend_flags() -> dict[str, bool]:
    """Which discovery backends are importable right now."""
    flags = {"visa": False, "serial": False, "zeroconf": False, "debug": False}
    try:
        import pyvisa  # noqa: F401

        flags["visa"] = True
    except ImportError:
        pass
    try:
        import serial.tools.list_ports  # noqa: F401

        flags["serial"] = True
    except ImportError:
        pass
    try:
        import zeroconf  # noqa: F401

        flags["zeroconf"] = True
    except ImportError:
        pass
    # The debug driver is always importable; whether a probe backend (probe-rs…)
    # is installed is discovered at call time.
    flags["debug"] = True
    return flags


def _transport_for_resource(resource: str) -> str:
    r = resource.upper()
    if r.startswith("USB"):
        return "usb-tmc"
    if r.startswith("GPIB"):
        return "gpib"
    if r.startswith("TCPIP"):
        return "lan"
    if r.startswith("ASRL") or r.startswith("SERIAL"):
        return "serial"
    return "other"


def _address_for_resource(resource: str) -> str:
    """The HIL address an agent should pass back to hil_exec for this resource."""
    return f"visa://{resource}"


def _visa_candidates() -> list[str]:
    try:
        import pyvisa  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        # '@py' = the pure-Python pyvisa-py backend; works without NI-VISA.
        try:
            rm = pyvisa.ResourceManager("@py")
        except Exception:  # noqa: BLE001 - fall back to the default backend
            rm = pyvisa.ResourceManager()
        resources = list(rm.list_resources("?*"))
        try:
            rm.close()
        except Exception:  # noqa: BLE001
            pass
        return [str(r) for r in resources]
    except Exception:  # noqa: BLE001 - a VISA error must not abort discovery
        return []


def _serial_candidates() -> list[dict]:
    try:
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except ImportError:
        return []
    out: list[dict] = []
    try:
        for p in list_ports.comports():
            out.append(
                {
                    "device": p.device,
                    "description": getattr(p, "description", "") or "",
                    "vid": getattr(p, "vid", None),
                    "pid": getattr(p, "pid", None),
                    "serial_number": getattr(p, "serial_number", None),
                }
            )
    except Exception:  # noqa: BLE001
        return []
    return out


def _debug_probes() -> list[dict]:
    """List on-chip debug probes via the debug driver (best-effort)."""
    try:
        from . import connect

        inst = connect("debug://probe-rs", timeout=15.0)
        try:
            probes = inst.probe_list()
        finally:
            try:
                inst.close()
            except Exception:  # noqa: BLE001
                pass
        return [p for p in probes if isinstance(p, dict)] or []
    except Exception:  # noqa: BLE001 - no probe backend, or none attached
        return []


def _lan_candidates(timeout: float) -> tuple[list[dict], list[str]]:
    """Browse LXI/VXI-11 over mDNS when zeroconf is available."""
    warnings: list[str] = []
    try:
        from zeroconf import ServiceBrowser, Zeroconf  # type: ignore[import-not-found]
    except ImportError:
        warnings.append(
            "lan: mDNS discovery needs 'zeroconf'; skipped. " + _backend_hint("lan")
            + " Or reach a LAN instrument directly with tcp://<ip>:5025."
        )
        return [], warnings

    found: dict[str, dict] = {}

    class _Listener:
        def add_service(self, zc: Any, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if info is None:
                return
            addresses = info.parsed_scoped_addresses() if hasattr(
                info, "parsed_scoped_addresses"
            ) else []
            host = addresses[0] if addresses else ""
            found[name] = {
                "name": name,
                "type": type_,
                "host": host,
                "port": info.port,
            }

        def remove_service(self, *a: Any) -> None:
            pass

        def update_service(self, *a: Any) -> None:
            pass

    zc = None
    try:
        zc = Zeroconf()
        ServiceBrowser(zc, ["_lxi._tcp.local.", "_vxi-11._tcp.local."], _Listener())
        import time

        time.sleep(min(timeout, 3.0))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"lan: mDNS browse failed ({exc}).")
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:  # noqa: BLE001
                pass
    return list(found.values()), warnings


def _identify(address: str, timeout: float) -> dict | None:
    """Best-effort *IDN? + identity parse for one address; None on any failure."""
    try:
        from . import connect

        inst = connect(address, timeout=timeout)
        try:
            raw = inst.idn()
            identity = inst.identify()
        finally:
            try:
                inst.close()
            except Exception:  # noqa: BLE001
                pass
        if not raw or not raw.strip():
            return None
        return {"idn": raw.strip(), "identity": identity}
    except Exception:  # noqa: BLE001 - an unreadable instrument is not fatal
        return None


def _match_reference(idn: str) -> tuple[str, str]:
    """(slug, name) of the best reference-corpus match for an *IDN? string.

    Only a *confident* match is returned; a vendor-only overlap (e.g. an unknown
    "Keysight" model hitting a different Keysight entry) is rejected so discovery
    never mislabels live hardware with the wrong profile."""
    if not idn:
        return "", ""
    try:
        from .reference import _confident, default_index

        best = default_index().best_match(idn)
        if best and _confident(best):
            return best.get("slug", ""), best.get("name", "")
    except Exception:  # noqa: BLE001 - reference lookup must never break discovery
        pass
    return "", ""


def discover(
    interfaces: tuple[str, ...] = DEFAULT_INTERFACES,
    *,
    identify: bool = True,
    timeout: float = 3.0,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Enumerate the live bench across the requested interfaces.

    ``interfaces`` is any subset of ``usb`` (VISA/USB-TMC/GPIB), ``serial``
    (RS-232 ports), ``lan`` (mDNS LXI/VXI-11) and ``debug`` (on-chip probes).
    ``identify`` performs a short ``*IDN?`` round-trip per candidate and matches
    it to the reference corpus. Returns ``{ok, devices, warnings, backends}``.
    """
    want = {i.strip().lower() for i in interfaces if i and i.strip()}
    if not want:
        want = set(DEFAULT_INTERFACES)
    flags = _backend_flags()
    devices: list[dict] = []
    warnings: list[str] = []
    progress = on_progress or (lambda _m: None)

    def _emit(msg: str) -> None:
        try:
            progress(msg)
        except Exception:  # noqa: BLE001
            pass

    # -- USB / VISA (USB-TMC, GPIB, TCPIP INSTR) ----------------------------
    if "usb" in want or "visa" in want:
        if flags["visa"]:
            _emit("scanning VISA/USB-TMC resources…")
            for resource in _visa_candidates():
                transport = _transport_for_resource(resource)
                if "usb" in want and transport not in ("usb-tmc", "gpib") and "visa" not in want:
                    # 'usb' alone should not pull in every TCPIP INSTR resource.
                    if transport == "lan":
                        continue
                rec: dict[str, Any] = {
                    "address": _address_for_resource(resource),
                    "transport": transport,
                    "backend": "visa",
                    "resource": resource,
                    "interface": "usb" if transport in ("usb-tmc", "gpib") else "lan",
                }
                if identify:
                    ident = _identify(rec["address"], timeout)
                    if ident:
                        rec.update(ident)
                        slug, name = _match_reference(ident.get("idn", ""))
                        rec["matched_slug"] = slug
                        rec["matched_name"] = name
                devices.append(rec)
        else:
            warnings.append(
                "usb: VISA backend not installed — USB-TMC/GPIB instruments are "
                "invisible. " + _backend_hint("usb")
            )

    # -- serial -------------------------------------------------------------
    if "serial" in want:
        if flags["serial"]:
            _emit("scanning serial ports…")
            for port in _serial_candidates():
                rec = {
                    "address": f"serial://{port['device']}",
                    "transport": "serial",
                    "backend": "pyserial",
                    "resource": port["device"],
                    "interface": "serial",
                    "description": port.get("description", ""),
                }
                if port.get("vid") is not None:
                    rec["vid"] = f"0x{port['vid']:04X}"
                if port.get("pid") is not None:
                    rec["pid"] = f"0x{port['pid']:04X}"
                if port.get("serial_number"):
                    rec["serial"] = port["serial_number"]
                devices.append(rec)
        else:
            warnings.append(
                "serial: pyserial not installed — RS-232/USB-serial ports are "
                "invisible. " + _backend_hint("serial")
            )

    # -- LAN / mDNS ---------------------------------------------------------
    if "lan" in want:
        _emit("browsing mDNS for LXI/VXI-11 instruments…")
        lan, lan_warnings = _lan_candidates(timeout)
        warnings.extend(lan_warnings)
        for svc in lan:
            host = svc.get("host", "")
            port = svc.get("port") or 5025
            rec = {
                "address": f"tcp://{host}:{port}" if host else "",
                "transport": "lan",
                "backend": "mdns",
                "resource": svc.get("name", ""),
                "interface": "lan",
            }
            if identify and host:
                ident = _identify(rec["address"], timeout)
                if ident:
                    rec.update(ident)
                    slug, name = _match_reference(ident.get("idn", ""))
                    rec["matched_slug"] = slug
                    rec["matched_name"] = name
            devices.append(rec)

    # -- debug probes -------------------------------------------------------
    if "debug" in want:
        _emit("listing on-chip debug probes…")
        for probe in _debug_probes():
            devices.append(
                {
                    "address": "debug://probe-rs",
                    "transport": "debug-probe",
                    "backend": probe.get("backend", "probe-rs"),
                    "resource": probe.get("serial") or probe.get("name", ""),
                    "interface": "debug",
                    "probe": probe,
                }
            )

    return {
        "ok": True,
        "count": len(devices),
        "interfaces": sorted(want),
        "backends": flags,
        "devices": devices,
        "warnings": warnings,
    }

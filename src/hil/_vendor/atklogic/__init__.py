"""Vendored core of ``atk-logic-mcp`` (MIT) — ATK-Logic-Analyzer device + decode layer.

Upstream: https://github.com/hugelzhang/atk-logic-mcp
Commit:   77320addfb34d3973d09a7af663de76023034587
License:  MIT (Copyright (c) 2026 hugelzhang) — see ``LICENSE`` and ``VENDORED.md``.

Modules
-------
``atkproto``
    USB protocol + device layer for the analyzer (VID:PID ``1A86:FFCC``). pyusb is
    imported lazily inside functions, so importing this package never requires pyusb.
``atkdl``
    Read/write the vendor ``.atkdl`` capture container (openable by ATK-Logic).
``srdhost``
    Pure-Python ``libsigrokdecode`` host that runs the vendor's 200+ sigrok PDs.

Only these three MIT modules are vendored — not upstream's MCP ``server.py``, its
tests, the Windows ``libusb-1.0.dll`` binary, docs, ``tools/`` or ``drivers/``.
Local modifications are minimal, marked ``# HIL-vendor:`` in-source, and listed in
``VENDORED.md``.
"""

from __future__ import annotations

from . import atkdl, atkproto, srdhost

__all__ = ["atkproto", "atkdl", "srdhost"]

"""Generate small, hermetic Framer-shaped fixtures for the scraper tests.

Run once to (re)create tests/fixtures/*.html:

    python tests/fixtures/_generate.py

The fixtures mirror the reference site's real structure (a flat ``framer/handover``
heap + an ``application/ld+json`` block) but are tiny and self-contained, so
tests never touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent

COMMANDS = [
    {
        "mnemonic": ":CLEar",
        "subsystem": "CLEar",
        "is_query": False,
        "anchor": "clear",
        "description": "Clears the screen.",
        "section": "3.1",
        "page": 10,
        "parameters": [],
        "returns": None,
        "examples": [],
    },
    {
        "mnemonic": ":MEASure:VPP",
        "subsystem": "MEASure",
        "is_query": True,
        "anchor": "measure-vpp",
        "description": "Measures peak-to-peak voltage.",
        "section": "3.4",
        "page": 22,
        "parameters": [
            {
                "name": "source",
                "type": "enum",
                "range_min": None,
                "range_max": None,
                "default": "CHAN1",
                "unit": None,
                "enum_values": ["CHAN1", "CHAN2"],
                "optional": True,
                "description": "Channel to measure.",
            }
        ],
        "returns": {
            "type": "float",
            "description": "Peak-to-peak volts.",
            "enum_values": [],
            "range_min": None,
            "range_max": None,
            "unit": "V",
        },
        "examples": [":MEASure:VPP? CHAN1 /*returns 3.3*/"],
    },
    {
        "mnemonic": ":CHANnel<n>:SCALe",
        "subsystem": "CHANnel<n>",
        "is_query": False,
        "anchor": "channel-scale",
        "description": "Sets vertical scale in volts per division.",
        "section": "3.2",
        "page": 14,
        "parameters": [
            {
                "name": "scale",
                "type": "float",
                "range_min": 0.001,
                "range_max": 1000.0,
                "default": None,
                "unit": "V",
                "enum_values": [],
                "optional": False,
                "description": "Volts per division.",
            }
        ],
        "returns": None,
        "examples": [],
    },
]

LDJSON = {
    "@context": "https://schema.org",
    "@graph": [
        {
            "@type": "TechArticle",
            "@id": "https://reference.invalid/equipment/instruments/fake-scope-1000#article",
            "headline": "Fake Scope 1000 SCPI Command Reference",
            "about": "Fake Scope 1000 SCPI remote-control command set",
            "articleSection": "Test & Measurement Automation",
            "keywords": ["Fake Scope 1000 SCPI", "FS1000 Series SCPI commands"],
            "isBasedOn": "FS1000 Programming Guide",
            "dateModified": "2026-01-02",
            "publisher": {"@type": "Organization", "name": "reference"},
        },
        {
            "@type": "Dataset",
            "@id": "https://reference.invalid/equipment/instruments/fake-scope-1000#dataset",
            "name": "Fake Scope 1000 SCPI command set",
            "description": (
                "3 SCPI commands across 3 subsystems for the Fake Scope 1000 "
                "(1 queries, 2 settable commands)."
            ),
            "variableMeasured": "SCPI command mnemonic, parameters, return values",
            "isBasedOn": "FS1000 Programming Guide",
        },
        {
            "@type": "BreadcrumbList",
            "@id": "https://reference.invalid/equipment/instruments/fake-scope-1000#crumb",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Equipment",
                 "item": "https://reference.invalid/equipment"},
                {"@type": "ListItem", "position": 2, "name": "Fake",
                 "item": "https://reference.invalid/equipment/vendors/fake"},
                {"@type": "ListItem", "position": 3, "name": "FS1000 Series",
                 "item": "https://reference.invalid/equipment/series/fs1000-series"},
            ],
        },
    ],
}

DESCRIPTION = (
    "The Fake Scope 1000 is a pretend oscilloscope in the FS1000 Series family "
    "used only by the HIL scraper test-suite. It exposes a handful of SCPI "
    "commands so parsing, YAML round-tripping and manifest roll-up can be "
    "exercised without any network access."
)


class Heap:
    """Build a Framer-style flat heap (children referenced by index)."""

    def __init__(self) -> None:
        self.items: list = []

    def add(self, value) -> int:
        self.items.append(value)
        return len(self.items) - 1

    def typed(self, type_name: str, value) -> int:
        v = self.add(value)
        t = self.add(type_name)
        return self.add({"type": t, "value": v})


def build_device_html() -> str:
    heap = Heap()
    # scalars, mirroring the real device page's top-level string/int elements
    heap.add("string")
    heap.add("Fake Scope 1000")
    heap.add("")
    heap.add("2026-01-02")
    heap.add(DESCRIPTION)
    heap.add(json.dumps(COMMANDS))
    heap.add(json.dumps(LDJSON))
    heap.add("number")
    heap.add(3)
    heap.add(2)
    heap.add(1)
    heap.add(2)
    heap.add("Fake")

    ld_text = json.dumps(LDJSON)
    return (
        "<!doctype html>\n<html><head>"
        '<script type="application/ld+json">' + ld_text + "</script>"
        '<script type="framer/handover" id="__framer__handoverData">'
        + json.dumps(heap.items)
        + "</script></head><body>"
        '<h1 class="framer-text">Fake Scope 1000</h1>'
        '<h2 class="framer-text">All commands</h2>'
        "</body></html>\n"
    )


def build_index_html() -> str:
    """Index fixture: a resolved heap holding per-category device records.

    Layout mirrors the real page: ``root = {"0": <list of records>, "1": <empty
    Map>}``, each record a dict of ``{slug,name,category,id}`` referencing
    scalar strings by index. Index 0 is reserved for the root, children are
    appended, then the root is filled in -- so every reference stays valid.
    """
    records = [
        {"slug": "fake-scope-1000", "name": "Fake Scope 1000",
         "category": "oscilloscope", "id": "aaa1"},
        {"slug": "fake-dmm-2000", "name": "Fake DMM 2000",
         "category": "multimeter", "id": "bbb2"},
        {"slug": "fake-psu-3000", "name": "Fake PSU 3000",
         "category": "power supply", "id": "ccc3"},
    ]
    heap: list = [None]  # reserve index 0 for the root

    def add(v) -> int:
        heap.append(v)
        return len(heap) - 1

    rec_idxs = []
    for r in records:
        s = add(r["slug"])
        n = add(r["name"])
        c = add(r["category"])
        i = add(r["id"])
        rec_idxs.append(
            add({"hpWTu_owi": s, "eRVE7hbFO": n, "YixQYNJC7": c, "id": i})
        )
    list_idx = add(rec_idxs)
    empty_map = add(["Map"])
    heap[0] = {"0": list_idx, "1": empty_map}

    links = "".join(
        f'<a href="./equipment/instruments/{r["slug"]}">{r["name"]}</a>'
        for r in records
    )
    return (
        "<!doctype html>\n<html><head>"
        '<script type="framer/handover" id="__framer__handoverData">'
        + json.dumps(heap)
        + "</script></head><body>" + links + "</body></html>\n"
    )


if __name__ == "__main__":
    (HERE / "reference_device.html").write_text(build_device_html(), encoding="utf-8")
    (HERE / "reference_index.html").write_text(build_index_html(), encoding="utf-8")
    print("wrote", HERE / "reference_device.html")
    print("wrote", HERE / "reference_index.html")

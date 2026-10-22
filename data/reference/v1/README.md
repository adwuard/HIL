# SCPI instrument reference corpus — v1

A versioned snapshot of the complete instrument command reference corpus,
assembled with `hil scrape`. Source URLs are sanitized (the origin host is
redacted). This directory is tracked in git; the working `scraped/` tree is not.

| | |
|---|---|
| version | **v1** (schema 1.0, hil 0.1.0) |
| generated | 2026-10-22T15:47:05+00:00 |
| devices | **725** (7 vendors, 17 categories) |
| commands | **294,478** (83,313 queries · 211,165 setters) |
| archive | `reference-corpus-v1.tar.gz` (45 MB, 1453 files) |
| sha256 | `9479585524a81e2b3024cc9e49a4006aacb22997924ced535981fa233cb54a1f` |

## Layout

```
data/reference/v1/
├── VERSION                       # stamp: counts, versions, archive sha256
├── README.md                     # this file
├── manifest.json                 # unpacked index: totals -> vendors -> categories -> devices
├── catalog.json                  # unpacked index: every device (slug · name · category · url)
└── reference-corpus-v1.tar.gz    # full corpus: manifest.json, catalog.json, devices/<slug>.{yaml,json}
```

`manifest.json` / `catalog.json` are kept unpacked so corpus-wide questions
("how many oscilloscopes? which Keysight SMUs?") need no extraction. The
per-device files (registry-loadable `.yaml` + lossless `.json`) live in the
archive.

## Unpack

```bash
tar -xzf data/reference/v1/reference-corpus-v1.tar.gz -C /path/to/dest
# -> dest/manifest.json  dest/catalog.json  dest/devices/<slug>.{yaml,json}
```

## Use one instrument

```python
from pathlib import Path
from hil.registry import load_device_file

dev = load_device_file(Path("dest/devices/rigol-dho1072.yaml"))  # ~0.2 s
print(len(dev.commands), dev.safety)   # 636 commands, safety preserved from seed
```

Scraped YAML is registry-compatible. To *drive* a real instrument, copy its
file into [`src/hil/devices/`](../../../src/hil/devices/) and give it a safety
policy first — a scrape never invents one.

## Regenerate / re-version

The corpus is reproducible from the site (or from the local HTTP cache, offline):

```bash
hil scrape all --out scraped --cache-dir .cache/reference --format both \
    --dataset-version v2                       # live re-crawl
hil scrape all --offline --out scraped --cache-dir .cache/reference \
    --format json --dataset-version v1         # rebuild from cache, no network
```

Then re-archive into `data/reference/<version>/` and refresh `VERSION`
(counts come from `manifest.json`; `sha256` from `shasum -a 256`).

## Integrity

`manifest.json` totals are derived from the actual per-device commands and
cross-checked against what each page advertises. Verify the archive:

```bash
shasum -a 256 data/reference/v1/reference-corpus-v1.tar.gz
# expect 9479585524a81e2b3024cc9e49a4006aacb22997924ced535981fa233cb54a1f
```

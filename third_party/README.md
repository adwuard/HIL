# `third_party/` — optional device backends, fetched on demand

HIL's **MIT** core works out of the box for the common case: SCPI instruments over
`tcp://`, `serial://`, and `visa://`. A handful of devices, though, need an
*optional* backend that HIL **deliberately does not bundle** — either because it
is GPL-licensed (and must not be linked into the MIT core) or because it is a
large native build. This folder is the integration point for those backends.

The golden rule: **you install HIL, and most things just work.** Only when you
actually reach for a device that needs an extra backend does HIL offer to fetch
or build it — and it never does so silently.

## What lives here

| Path | Purpose |
| --- | --- |
| `bootstrap.sh` | A thin wrapper around `hil setup` for a fresh clone (no venv needed). |
| `recipes/` | Lab-local provisioning recipes (`*.yaml`). These **merge over** the bundled recipes by slug, so you can override or add a device without editing HIL. |
| `work/` | Where clones, native builds, and downloads land. **Git-ignored** and created lazily. Override with `$HIL_PROVISION_WORK`. |

The recipes themselves ship inside the package at
[`src/hil/provision/recipes/`](../src/hil/provision/recipes) so `hil setup` also
works from an installed wheel. Discovery merge order (later wins on slug):

1. `src/hil/provision/recipes/` — bundled with HIL
2. `third_party/recipes/` — this folder, your overrides
3. `$HIL_RECIPES_DIR` — an extra folder, if set

## The two bundled backends

| Recipe | Driver | License | Shape |
| --- | --- | --- | --- |
| `ppk2` | `ppk2` | GPLv2 | Pure Python: `pip install ppk2-api` + the native `libusb` runtime. No compilation. |
| `alientek-atk-logic` | `logic` | GPLv3 | Native build: a Qt/`libusb`/`glib` toolchain, a `git clone`, then `qmake`/`make`. Cross-compiles per the `.pro` platform scopes. |

HIL's MIT drivers for these devices are thin clients (a pseudo-SCPI PPK2 facade;
a JSON-RPC logic-daemon client) — the GPL code stays out of process and out of
the core.

## How provisioning is triggered

All four entry points run the **same** engine (`hil.provision`), so behavior is
identical everywhere:

```sh
hil setup                       # status of every backend (nothing is changed)
hil setup --list                # list recipes
hil setup ppk2                  # fetch + install the PPK2 backend
hil setup logic --dry-run       # print the whole plan, run nothing
hil setup alientek-atk-logic --yes   # allow privileged deps + native build
hil doctor                      # env + backend availability, incl. provisioning

third_party/bootstrap.sh ppk2   # same thing, from a fresh clone

HIL_AUTO_PROVISION=1 python -c "import hil; hil.connect('ppk2:///dev/cu.usbmodem01')"
# connect() auto-runs the *non-privileged, non-heavy* steps, then connects.
```

When a backend is missing, `hil.connect()` and the driver errors point you at the
exact command (`→ … set it up with: hil setup <slug>`).

## The safety model

Provisioning can install packages and compile code, so it is conservative by
default:

- **Privileged steps** (system package installs that may need `sudo`) **never run
  unless you pass `--yes`.** Otherwise they are reported as `gated` with the
  exact command to re-run yourself.
- **Heavy steps** (a long native compile) are **also gated in auto mode**, so an
  implicit `hil.connect()` under `HIL_AUTO_PROVISION=1` never stalls on a
  multi-minute build. Run `hil setup <slug> --yes` to compile explicitly.
- **Dry-run** (`--dry-run`) prints every command without executing anything.
- A failing step aborts the run unless it is marked `optional`.

In other words, `HIL_AUTO_PROVISION=1` auto-runs only the safe, unattended parts
— `pip` installs and `git` clones. Anything that touches system packages or a
compiler waits for an explicit, confirmed `hil setup … --yes`.

## Cross-compilation

The ATK-Logic `.pro` file carries `win32` / `macx` / `unix` / `android`(NDK)
scopes. Set `HIL_CROSS_TARGET` (e.g. `android-arm64`) plus the matching toolchain
prefix so the build steps target the right ABI:

```sh
HIL_CROSS_TARGET=android-arm64 hil setup alientek-atk-logic --yes
```

## Adding or overriding a recipe

Drop a `*.yaml` file in `third_party/recipes/`. A recipe has three parts —
`detect` (is it already usable?), `steps` (how to fetch/build it), and `verify`
(did it work?). Minimal example:

```yaml
slug: my-scope              # lowercase/digits/hyphens; overrides a bundled slug
driver: scpi                # which driver this backend serves
description: My vendor scope SDK
license: Proprietary
homepage: https://example.invalid/my-scope
workdir: my-scope           # relative to the work root

detect:                     # "ready" == every detect probe passes
  - python_import: myscope_sdk

steps:
  - id: install-sdk
    pip: { packages: [myscope-sdk] }
  - id: native-lib          # privileged: gated behind --yes
    system:
      macos:  { brew: [libfoo] }
      linux:  { apt-get: [libfoo-dev] }
  - id: build               # heavy: never started implicitly (auto mode)
    heavy: true
    needs: [make]
    shell:
      default: cd src && make

verify:
  - python_import: myscope_sdk
```

Probe kinds: `python_import`, `binary`, `command`, `daemon`. Step actions: `pip`,
`system`, `git`, `shell`. Step flags: `privileged`, `heavy`, `optional`, `only_os`,
`needs`. The full model is documented in
[`src/hil/provision/recipe.py`](../src/hil/provision/recipe.py) and interpreted by
[`src/hil/provision/engine.py`](../src/hil/provision/engine.py).

## Environment variables

| Variable | Effect |
| --- | --- |
| `HIL_AUTO_PROVISION` | When truthy, `hil.connect()` auto-runs non-privileged, non-heavy steps. |
| `HIL_PROVISION_WORK` | Where clones/builds/downloads land (default: `third_party/work`). |
| `HIL_RECIPES_DIR` | An extra recipe folder, merged last (wins on slug). |
| `HIL_CROSS_TARGET` | Cross-compile target for native builds (e.g. `android-arm64`). |

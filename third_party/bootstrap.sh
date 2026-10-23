#!/usr/bin/env bash
# third_party/bootstrap.sh — a thin wrapper around `hil setup`.
#
# HIL's MIT core works out of the box for the common case (SCPI over tcp/serial/
# visa). A few devices need an *optional* backend HIL deliberately does not
# bundle — the Nordic PPK2 (GPLv2 `ppk2-api`) and the ALIENTEK ATK-Logic capture
# daemon (GPLv3 Qt/libusb native build). This script fetches/builds them on
# demand. All real logic lives in the Python engine (`hil.provision`), so the
# behavior is identical across this script, the `hil setup` CLI, `hil doctor`,
# and the automatic on-demand hook in `hil.connect()` (HIL_AUTO_PROVISION=1).
#
# Usage:
#   third_party/bootstrap.sh                     # status of every backend
#   third_party/bootstrap.sh --list              # list provisioning recipes
#   third_party/bootstrap.sh ppk2                # fetch + install the PPK2 backend
#   third_party/bootstrap.sh logic --dry-run     # print the plan, run nothing
#   third_party/bootstrap.sh alientek-atk-logic --yes   # allow privileged deps + build
#   HIL_CROSS_TARGET=android-arm64 third_party/bootstrap.sh logic --yes
#
# Safety: privileged (sudo) steps and long native compiles never run implicitly;
# they require --yes. Without it they are reported as *gated* with the exact
# command to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
# Make the in-repo source importable when HIL is not installed into the env.
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"

# 1) An installed console script is the happiest path.
if command -v hil >/dev/null 2>&1; then
  exec hil setup "$@"
fi

# 2) A project-local venv (uv/pip) console script.
if [ -x "$ROOT/.venv/bin/hil" ]; then
  exec "$ROOT/.venv/bin/hil" setup "$@"
fi

# 3) Fall back to running the CLI module directly from source.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
  else
    PY="$(command -v python3 || command -v python || true)"
  fi
fi
if [ -z "$PY" ]; then
  echo "bootstrap: no 'hil' on PATH and no python3 found." >&2
  echo "           Install HIL (pip install -e .) or set PYTHON=/path/to/python." >&2
  exit 127
fi

exec "$PY" -c 'import sys
from hil.cli import main
raise SystemExit(main(["setup", *sys.argv[1:]]))' "$@"

![HIL — Hardware-in-the-Loop testing for lab instruments](assets/hil-hero.png)

# HIL

### Hardware-in-the-Loop testing for lab instruments

**One safe, machine-readable interface to your bench.** Drive oscilloscopes,
source-measure units, power supplies, waveform generators, DMMs and e-fuses from
Python, the CLI, or an AI agent — over LAN, serial, or GPIB — with per-instrument
safety enforced by default.

```bash
pip install git+https://github.com/adwuard/HIL.git     # active development — not on PyPI yet
```

> **Active development.** HIL is pre-release (v0.1.0) and is **not published to PyPI** yet —
> the `hil` name there belongs to an unrelated project, so HIL will publish as
> **`hil-harness`**. Install from source (above); the API may change between commits.

**725** instruments · **7** vendors · **~294k** SCPI commands · runs with **zero** hardware

---

## Why

Every instrument speaks SCPI — but every vendor speaks it *differently*. Different
commands, different connection methods, different ways to ruin a bench if you send
the wrong thing at the wrong time. Pointing a script (or an autonomous agent) at
real hardware means hand-wiring sockets, VISA resources, vendor dialects and safety
limits, every single time.

**HIL is the layer that ends that.** A uniform transport, a typed SCPI instrument, a
device registry with built-in safety policies, and a declarative test bench — all
surfaced as clean JSON that a human *or* a model can drive.

## What's inside

| Layer | Module | What it gives you |
|---|---|---|
| **Transport** | `hil.transports` | One byte-pipe to the instrument: raw **TCP** socket (stdlib, always on), **RS-232 serial**, and **VISA** (GPIB / USB / Ethernet). Optional backends install as extras, so the base stays light. |
| **Instrument** | `hil.scpi` | `ScpiInstrument` — `write` / `query` / `execute`, the IEEE-488.2 commons (`*IDN?`, `*OPC?`, `*RST`), error-queue draining, and `safe_execute` that returns a structured result instead of raising. |
| **Knowledge** | `hil.registry` | Declarative **YAML device profiles**: capabilities, command sets, connection examples, and a **safety policy** (block / allow / limits). Drop in your own dirs to extend it. |
| **Addressing** | `hil.addresses` | A single resolver for `tcp://host:5025`, `serial:///dev/ttyUSB0?baud=9600`, `visa://GPIB::24::INSTR`, a bare `GPIB::24::INSTR`, or a registry **slug**. |
| **Errors** | `hil.errors` | A typed hierarchy where every message is *agent-actionable*: what failed, why, and the next step. |
| **Bench** | `hil.bench` | Declarative test **procedures** + a deterministic **simulator** + assertions + reports. Safety-first, and it runs with **zero hardware**. |
| **Interface** | `hil.cli` | An agent-first CLI — every command emits stable `--json` with meaningful exit codes. |

## Built to be driven by AI

HIL isn't a GUI wrapper — it's a **Hardware-in-the-Loop harness**: the safe, structured surface an LLM
agent needs to operate real instruments autonomously.

- **Structured everything.** Every CLI command has `--json`; errors are typed and
  self-describing; `safe_execute` returns `{ok, response, errors, elapsed_s}` rather
  than throwing. The model gets parseable facts, not stack traces.
- **Safety by default.** Each device profile carries a policy. The bench *refuses to
  start* if a procedure could issue a blocked command, and re-checks every step
  against the policy before it reaches hardware — so an agent can explore freely
  without risking the bench.
- **A simulator to rehearse on.** A deterministic in-memory instrument — replies
  configured per procedure — lets agent-authored procedures run end-to-end with no
  hardware, so plans are validated before they touch a real device.
- **Plans as data.** A test is a versionable YAML procedure — auditable, reproducible,
  diffable — which is exactly how you want an agent's intent recorded.
- **MCP server (roadmap).** The same primitives, exposed as native model tools.

## Use it

HIL covers both halves of instrument work: **develop** — write code that talks to a
bench — and **test** — prove behaviour with repeatable, safety-checked procedures.

### Develop — one API over any instrument

```python
from hil import connect

smu = connect("keithley-2400")            # by registry slug -> safety policy attached
#   connect("tcp://192.168.1.5:5025")     # or by raw address

print(smu.idn())                          # 'KEITHLEY INSTRUMENTS,2400,...'
smu.write(":SOUR:VOLT 3.3")               # blocked if it violates the policy
print(smu.query(":MEAS:VOLT?"))
print(smu.safe_execute(":OUTP ON"))       # {'ok': True, 'errors': [], ...}
```

No hardware on hand? Develop against the in-memory instrument — same API, and it
records every command your code actually sent:

```python
from hil.bench import SimTransport
from hil.scpi import ScpiInstrument

sim = SimTransport(responses={":MEAS:VOLT?": "4.998"}, idn="KEITHLEY,2400,SN1,C33")
smu = ScpiInstrument(sim)
smu.write(":SOUR:VOLT 5.0")
print(smu.query(":MEAS:VOLT?"))   # 4.998
print(sim.writes)                 # [':SOUR:VOLT 5.0', ':MEAS:VOLT?']
```

Same thing from the shell (every command also takes `--json`):

```bash
hil idn tcp://192.168.1.5:5025            # identify
hil ask keithley-2400 ":MEAS:VOLT?"       # query (safety-checked)
hil exec keithley-2400 ":SOUR:VOLT 3.3"   # write (safety-checked)
hil devices list --json                   # the registry, machine-readable
```

### Test — repeatable procedures, hardware optional

```bash
hil bench init my-test                    # scaffold a procedure
hil bench validate my-test.yaml           # static checks, no instrument needed
hil bench run my-test.yaml --sim          # dry-run on the simulator
hil bench run my-test.yaml --address tcp://192.168.1.5:5025   # for real
```

```yaml
name: smu-output-accuracy
safety: { blocked: ["CAL:STR:UNPR"] }     # uncalibrate can never be issued
steps:
  - rst: {}
  - write: { command: ":SOUR:VOLT 5.0" }
  - write: { command: ":OUTP ON" }
  - query: { command: ":MEAS:VOLT?", save: vout }
  - assert: { kind: close, value: "{{ vout }}", expect: 5.0, tol: "1%" }
```

Run it with zero hardware — the simulator answers, and the safety policy still
applies exactly as it would on the bench:

```
$ hil bench run smu-output-accuracy.yaml --sim
PASS  SMU output accuracy  [sim: simulator]  device=keithley-2400
  10/10 steps passed
  measurements: vout=3.301 V, iout=0.052 A
```

## Knowledge base

HIL ships with a **reference library of 725 instruments across 7 vendors** — roughly
**294,000 SCPI commands** (83k queries · 211k setters) spanning 17 categories:
oscilloscopes, SMUs, power sources, meters, RF, DAQ, electronic loads and e-fuses.
Four profiles are hand-curated with safety policies and load by default; the full
corpus is a versioned dataset under `data/` that you can add to the registry.

## Install

HIL is in **active development** and is not on PyPI yet — the `hil` name there is an
unrelated project. Install from source:

```bash
pip install git+https://github.com/adwuard/HIL.git      # latest main
# or clone and install editable for development:
git clone https://github.com/adwuard/HIL.git && cd HIL
pip install -e '.[dev]'                                  # editable + test deps
```

Extras: `.[serial]` (RS-232), `.[visa]` (GPIB / VISA / USB), `.[all]`, `.[dev]`.
Python 3.10+. Core requires only PyYAML.

## Status

**v0.1.0** — transports, SCPI layer, registry, bench and CLI are live and tested.
The MCP server and a PyPI release as `hil-harness` are next.
MIT licensed.

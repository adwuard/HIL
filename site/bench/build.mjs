// Anatomy build: the HIL multi-workbench isometric figure.
// Imports the installed Anatomy iso-kit and writes:
//   ../assets/iso.css         the kit's structural CSS (palette overridden in styles.css)
//   ../assets/bench-scenes.js window.__HIL_BENCH = [ {id,name,tag,hint,readout,keys,parts,svg}, ... ]
// Run: node site/bench/build.mjs
import { writeFileSync, mkdirSync, readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as k from "../../.claude/skills/anatomy/kit/iso-kit.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS = join(HERE, "..", "assets");
mkdirSync(ASSETS, { recursive: true });

const WIDTH = 820;
const HEIGHT = 470;

// Shared world layout: one bench, one camera, so switching workbenches stays coherent.
const DECK = { x: -212, y: -152, w: 424, d: 302, r: 16 };
const DECK_H = 20;
const FOOT_H = 12;

const AGENT = { x: -184, y: -124, w: 78, d: 56, r: 5 };
const AGENT_H = 26;
const HUB = { x: -70, y: -126, w: 152, d: 62, r: 6 };
const HUB_H = 30;

const SLOT_A = { x: 96, y: -122, w: 106, d: 68, r: 5 };   // right, back
const SLOT_B = { x: 78, y: 4, w: 120, d: 82, r: 6 };      // right, front
const SLOT_C = { x: -198, y: 0, w: 100, d: 80, r: 5 };    // left, front
const DUT = { x: -58, y: 38, w: 100, d: 68, r: 3 };       // front, centre

const P = k.fitProjection(
  [...k.boxCorners(DECK, -(DECK_H + FOOT_H), 0), [HUB.x + 20, HUB.y + 10, 62], [SLOT_A.x + 40, SLOT_A.y + 10, 62]],
  WIDTH,
  HEIGHT,
  { pad: 34, azimuth: 42 },
);

const at = (p) => k.iso(p, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const D = (pts, style) => k.dotsSvg(pts, style);
const W = (from, to, style) => k.wireSvg(from, to, style);
const FT = (m, text, o) => k.faceTextSvg(m, text, o);
const depth = (p) => k.depthOf([p[0], p[1], p[2] || 0], P);
const byDepth = (pts) => [...pts].sort((a, b) => depth([a[0], a[1], 0]) - depth([b[0], b[1], 0]));

function painter() {
  const items = [];
  return {
    put(svg, anchor, bias = 0) {
      if (svg) items.push({ key: depth(anchor) + bias, svg });
    },
    done() {
      items.sort((a, b) => a.key - b.key);
      return items.map((i) => i.svg).join("");
    },
  };
}

const centre = (plan) => [plan.x + plan.w / 2, plan.y + plan.d / 2];

// ---- shared bench elements -------------------------------------------------

function deck(pt) {
  const g = [`<path class="iso-halo" d="${k.haloOf(DECK, -(DECK_H + FOOT_H), DECK_H, P)}"/>`];
  for (const [x, y] of byDepth(k.corners(DECK, 30))) {
    g.push(S(k.cylinder(x, y, 7, -(DECK_H + FOOT_H), FOOT_H, P, 22), { tone: "lo" }));
  }
  g.push(S(k.slabOf(DECK, -DECK_H, DECK_H, P, 10, 1.3), { tone: "mid" }));
  g.push(L(k.planOutline(k.insetPlan(DECK, 7), 0.1, P, 10), { tone: "faint" }));
  g.push(L(k.planOutline(k.insetPlan(DECK, 15), 0.1, P, 10), { tone: "faint" }));
  // bench mat perforation, kept faint
  const holes = [];
  for (let ix = -180; ix <= 180; ix += 30) for (let iy = -120; iy <= 120; iy += 30) holes.push(at([ix, iy, 0.2]));
  g.push(D(holes, { size: 0.35, tone: "faint" }));
  // the deck is the ground plane: force it to paint before every object on it
  pt.put(`<g data-part="bench">${g.join("")}</g>`, [0, 120, -DECK_H], -1e6);
}

function screenOn(plan, z, text, o = {}) {
  const sw = o.w || plan.w * 0.6;
  const sh = o.h || 20;
  const sp = { x: o.x ?? centre(plan)[0] - sw / 2, y: o.y ?? plan.y + 7, w: sw, d: sh, r: 2 };
  const out = [S(k.slabOf(sp, z, 1.3, P, 3), { tone: "lo", crease: "none" })];
  out.push(L(k.planOutline(k.insetPlan(sp, 1.6), z + 1.4, P, 3), { tone: "faint" }));
  if (text) {
    out.push(FT(k.topMatrix([sp.x + sp.w / 2, sp.y + sh / 2 + 1], z + 1.5, P, "x"), text, { size: o.size || 4.4, tone: o.tone || "blue", anchor: "middle" }));
  }
  return { svg: out.join(""), plan: sp };
}

function knob(x, y, z, r) {
  return [
    S(k.cylinder(x, y, r, z, 4.5, P, 26, 0.4), { tone: "hi" }),
    L(k.knurl(x, y, r, z, z + 4.5, Math.max(12, Math.round(r * 6)), P, 0.35), { tone: "lo" }),
    L(k.lineOnTop([x, y], [x + r * 0.78, y], z + 4.6, P), { tone: "lit" }),
  ].join("");
}

function agent(pt) {
  const g = [S(k.slabOf(AGENT, 0, AGENT_H, P, 6, 0.7), { tone: "mid" })];
  g.push(L(k.planOutline(k.insetPlan(AGENT, 3), AGENT_H, P, 6), { tone: "faint" }));
  g.push(screenOn(AGENT, AGENT_H, "agent", { tone: "lit", w: AGENT.w - 18, h: 24, size: 5 }).svg);
  g.push(D([at([AGENT.x + 9, AGENT.y + AGENT.d - 9, AGENT_H + 0.4])], { size: 1.1, tone: "lit", pulse: true }));
  pt.put(`<g data-part="agent">${g.join("")}</g>`, centre(AGENT).concat(0));
}

function hub(pt) {
  const g = [S(k.slabOf(HUB, 0, HUB_H, P, 8, 0.8), { tone: "mid" })];
  g.push(L(k.planOutline(k.insetPlan(HUB, 4), HUB_H, P, 8), { tone: "faint" }));
  g.push(FT(k.topMatrix([HUB.x + 30, HUB.y + 20], HUB_H + 0.4, P, "x"), "HIL", { size: 8, tone: "hi", anchor: "middle" }));
  g.push(FT(k.topMatrix([HUB.x + 30, HUB.y + 34], HUB_H + 0.4, P, "x"), "harness", { size: 3.4, tone: "mid", anchor: "middle" }));
  g.push(screenOn({ ...HUB, x: HUB.x + 62, w: HUB.w - 70 }, HUB_H, "hil> safe_execute", { x: HUB.x + 62, y: HUB.y + 9, w: HUB.w - 74, h: 20, tone: "lit", size: 3.8 }).svg);
  // safety LED (ok) + a port row
  g.push(D([at([HUB.x + 12, HUB.y + HUB.d - 11, HUB_H + 0.4])], { size: 1.2, tone: "green", pulse: true }));
  const ports = [];
  for (let i = 0; i < 7; i++) ports.push(at([HUB.x + 66 + i * 11, HUB.y + HUB.d - 9, HUB_H + 0.4]));
  g.push(D(ports, { size: 0.7, tone: "mid" }));
  pt.put(`<g data-part="hub">${g.join("")}</g>`, centre(HUB).concat(0));
}

// A PCB: thin board with a chip, a header and a couple of parts.
function pcb(pt, id, plan, o = {}) {
  const h = o.h || 5;
  const g = [S(k.slabOf(plan, 0, h, P, 3, 0.5), { tone: "mid" })];
  g.push(L(k.planOutline(k.insetPlan(plan, 2.4), h, P, 3), { tone: "faint" }));
  const [cx, cy] = centre(plan);
  const chip = { x: cx - 13, y: cy - 12, w: 26, d: 24, r: 1.5 };
  g.push(S(k.slabOf(chip, h, o.chipH || 6, P, 2, 0.4), { tone: "lo" }));
  g.push(L(k.planOutline(k.insetPlan(chip, 3), h + (o.chipH || 6), P, 2), { tone: "faint" }));
  // header pins along one edge
  const pins = [];
  for (let i = 0; i < 8; i++) pins.push(at([plan.x + 8 + i * ((plan.w - 16) / 7), plan.y + plan.d - 5, h + 0.4]));
  g.push(D(pins, { size: 0.55, tone: "hi" }));
  // a couple of small parts
  g.push(S(k.cylinder(plan.x + plan.w - 14, plan.y + 12, 3.4, h, 4, P, 18), { tone: "hi" }));
  g.push(S(k.slabOf({ x: plan.x + 9, y: plan.y + 9, w: 12, d: 7, r: 1 }, h, 3, P, 2), { tone: "hi" }));
  if (o.label) g.push(FT(k.topMatrix([cx, plan.y + plan.d - 15], h + 0.3, P, "x"), o.label, { size: 3.6, tone: o.labelTone || "mid", anchor: "middle" }));
  if (o.led) g.push(D([at([plan.x + plan.w - 8, plan.y + plan.d - 8, h + 0.5])], { size: 1, tone: o.led, pulse: true }));
  pt.put(`<g data-part="${id}">${g.join("")}</g>`, [cx, cy, 0]);
}

// A rack instrument: body + top screen + knobs + label + status LED.
function instrument(pt, id, plan, o = {}) {
  const h = o.h || 30;
  const g = [S(k.slabOf(plan, 0, h, P, 6, 0.7), { tone: "mid" })];
  g.push(L(k.planOutline(k.insetPlan(plan, 3.2), h, P, 6), { tone: "faint" }));
  const [cx, cy] = centre(plan);
  if (o.screen !== false) {
    const sc = screenOn(plan, h, o.screen, { w: o.screenW || plan.w * 0.58, h: o.screenH || 22, tone: o.screenTone || "blue", size: o.screenSize || 4.4, x: plan.x + 8, y: plan.y + 8 });
    g.push(sc.svg);
    if (o.wave) g.push(wave(sc.plan, h + 1.6, o.waveTone || "blue"));
  }
  (o.knobs || []).forEach(([ox, oy, r]) => g.push(knob(plan.x + ox, plan.y + oy, h, r)));
  if (o.buttons) {
    for (let i = 0; i < o.buttons; i++) {
      g.push(S(k.slabOf({ x: plan.x + plan.w - 14, y: plan.y + 12 + i * 11, w: 8, d: 8, r: 1.5 }, h, 2, P, 2), { tone: "hi" }));
    }
  }
  if (o.label) g.push(FT(k.topMatrix([cx, plan.y + plan.d - 8], h + 0.3, P, "x"), o.label, { size: 3.8, tone: o.labelTone || "mid", anchor: "middle" }));
  if (o.led) g.push(D([at([plan.x + 9, plan.y + plan.d - 9, h + 0.4])], { size: 1, tone: o.led, pulse: true }));
  pt.put(`<g data-part="${id}">${g.join("")}</g>`, [cx, cy, 0]);
}

function wave(plan, z, tone = "blue") {
  const n = 12;
  const x0 = plan.x + 4;
  const x1 = plan.x + plan.w - 4;
  const mid = plan.y + plan.d / 2;
  const amp = plan.d * 0.26;
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    pts.push([x0 + (x1 - x0) * t, mid + Math.sin(t * Math.PI * 4) * amp]);
  }
  let d = "";
  for (let i = 0; i < pts.length - 1; i++) d += k.lineOnTop(pts[i], pts[i + 1], z, P);
  return L(d, { tone });
}

// A file/disk artifact block (capture spool).
function artifact(pt, id, plan, o = {}) {
  const h = o.h || 10;
  const g = [S(k.slabOf(plan, 0, h, P, 3, 0.5), { tone: "lo" })];
  g.push(L(k.planOutline(k.insetPlan(plan, 3), h, P, 3), { tone: "faint" }));
  for (let i = 1; i <= 3; i++) g.push(L(k.lineOnTop([plan.x + 6, plan.y + i * (h / 4)], [plan.x + plan.w - 6, plan.y + i * (h / 4)], h + 0.1, P), { tone: "faint" }));
  const [cx, cy] = centre(plan);
  g.push(FT(k.topMatrix([cx, plan.y + plan.d - 7], h + 0.3, P, "x"), o.label || "artifact", { size: 3.4, tone: "mid", anchor: "middle" }));
  if (o.led) g.push(D([at([plan.x + plan.w - 8, plan.y + 8, h + 0.4])], { size: 0.9, tone: o.led, pulse: true }));
  pt.put(`<g data-part="${id}">${g.join("")}</g>`, [cx, cy, 0]);
}

// Cables: screen-space connectors with terminal dots and animated flow.
function cable(pt, fromWorld, toWorld, viaWorld, tone, bias = 0) {
  const svg = W(at(fromWorld), at(toWorld), { via: viaWorld.map((v) => at(v)), tone, flow: true, ends: "both", size: 1.5 });
  const mid = [(fromWorld[0] + toWorld[0]) / 2, (fromWorld[1] + toWorld[1]) / 2, (fromWorld[2] + toWorld[2]) / 2 + 6];
  pt.put(svg, mid, bias);
}

// ---- the four workbenches --------------------------------------------------

function sceneSCPI() {
  const pt = painter();
  deck(pt);
  // instrument -> hub -> agent wiring (drawn under the boxes)
  cable(pt, [AGENT.x + AGENT.w - 6, AGENT.y + AGENT.d / 2, AGENT_H], [HUB.x + 6, HUB.y + HUB.d / 2, HUB_H], [[-98, -95, 30]], "lit", -0.5);
  cable(pt, [HUB.x + HUB.w - 8, HUB.y + 14, HUB_H], [SLOT_A.x + 18, SLOT_A.y + SLOT_A.d - 4, 34], [[88, -70, 40]], "blue", -0.5);
  cable(pt, [HUB.x + HUB.w - 8, HUB.y + HUB.d - 12, HUB_H], [SLOT_B.x + 20, SLOT_B.y + 6, 34], [[80, -20, 42]], "blue", -0.5);
  cable(pt, [HUB.x + 40, HUB.y + HUB.d - 4, HUB_H], [DUT.x + 20, DUT.y + 6, 6], [[-8, -6, 34]], "blue", -0.5);
  agent(pt);
  hub(pt);
  instrument(pt, "supply", SLOT_C, { h: 30, screen: "3.30 V  1.05 A", label: "ea-el9000b", led: "blue", knobs: [[24, 52, 6], [50, 54, 5]] });
  instrument(pt, "scope", SLOT_A, { h: 34, screen: "", wave: true, screenW: SLOT_A.w - 20, screenH: 30, label: "rigol dho1072", led: "blue", knobs: [[SLOT_A.w - 20, 22, 6], [SLOT_A.w - 20, 46, 5]] });
  instrument(pt, "smu", SLOT_B, { h: 32, screen: "4.998 V", screenW: SLOT_B.w * 0.5, label: "keithley 2400", led: "green", knobs: [[SLOT_B.w - 26, 26, 8], [SLOT_B.w - 24, 56, 5]], buttons: 3 });
  pcb(pt, "dut", DUT, { label: "DUT", led: "lit" });
  return pt.done();
}

function sceneDebug() {
  const pt = painter();
  deck(pt);
  cable(pt, [AGENT.x + AGENT.w - 6, AGENT.y + AGENT.d / 2, AGENT_H], [HUB.x + 6, HUB.y + HUB.d / 2, HUB_H], [[-98, -95, 30]], "lit", -0.5);
  cable(pt, [HUB.x + HUB.w - 8, HUB.y + 16, HUB_H], [SLOT_A.x + 18, SLOT_A.y + SLOT_A.d - 6, 22], [[92, -76, 34]], "blue", -0.5);
  cable(pt, [SLOT_A.x + 30, SLOT_A.y + SLOT_A.d - 4, 20], [DUT.x + 30, DUT.y + 6, 8], [[60, -6, 30]], "blue", 0.2);
  cable(pt, [HUB.x + 40, HUB.y + HUB.d - 4, HUB_H], [SLOT_C.x + SLOT_C.w - 16, SLOT_C.y + 8, 8], [[-40, -20, 34]], "blue", -0.5);
  agent(pt);
  hub(pt);
  instrument(pt, "probe", SLOT_A, { h: 22, screen: "probe-rs", screenTone: "lit", screenW: SLOT_A.w - 22, screenH: 24, label: "SWD / JTAG", led: "green", knobs: [] });
  pcb(pt, "target", DUT, { label: "nrf52840", led: "lit", chipH: 7 });
  pcb(pt, "esp", SLOT_C, { h: 6, label: "esp32-devkit", led: "blue", chipH: 8 });
  instrument(pt, "rtt", SLOT_B, { h: 26, screen: "RTT  ok", screenTone: "lit", screenW: SLOT_B.w * 0.5, label: "gdb + rtt", led: "blue", knobs: [[SLOT_B.w - 24, 40, 6]] });
  return pt.done();
}

function sceneLogic() {
  const pt = painter();
  deck(pt);
  cable(pt, [AGENT.x + AGENT.w - 6, AGENT.y + AGENT.d / 2, AGENT_H], [HUB.x + 6, HUB.y + HUB.d / 2, HUB_H], [[-98, -95, 30]], "lit", -0.5);
  cable(pt, [HUB.x + HUB.w - 8, HUB.y + 16, HUB_H], [SLOT_A.x + 18, SLOT_A.y + SLOT_A.d - 6, 26], [[92, -74, 36]], "blue", -0.5);
  cable(pt, [SLOT_A.x + 24, SLOT_A.y + SLOT_A.d - 4, 24], [DUT.x + 34, DUT.y + 8, 8], [[64, -4, 30]], "blue", 0.2);
  cable(pt, [HUB.x + HUB.w - 8, HUB.y + HUB.d - 12, HUB_H], [SLOT_B.x + 16, SLOT_B.y + 8, 14], [[86, -20, 30]], "blue", -0.5);
  agent(pt);
  hub(pt);
  instrument(pt, "la", SLOT_A, { h: 26, screen: "4 MS/s", screenW: SLOT_A.w * 0.5, label: "atk-logic", led: "blue", knobs: [], buttons: 0 });
  // probe wire bundle from analyzer to the DUT
  pcb(pt, "dut", DUT, { label: "DUT  i2c", led: "lit" });
  artifact(pt, "capture", SLOT_B, { h: 12, label: "cap.atkdl", led: "green" });
  instrument(pt, "decode", SLOT_C, { h: 26, screen: "i2c  128", screenTone: "lit", screenW: SLOT_C.w - 20, screenH: 24, label: "decode", led: "blue", knobs: [] });
  return pt.done();
}

function scenePower() {
  const pt = painter();
  deck(pt);
  cable(pt, [AGENT.x + AGENT.w - 6, AGENT.y + AGENT.d / 2, AGENT_H], [HUB.x + 6, HUB.y + HUB.d / 2, HUB_H], [[-98, -95, 30]], "lit", -0.5);
  cable(pt, [HUB.x + HUB.w - 8, HUB.y + 16, HUB_H], [SLOT_A.x + 18, SLOT_A.y + SLOT_A.d - 6, 26], [[92, -74, 36]], "blue", -0.5);
  cable(pt, [SLOT_A.x + 26, SLOT_A.y + SLOT_A.d - 4, 24], [DUT.x + 30, DUT.y + 6, 8], [[62, -6, 30]], "lit", 0.2);
  cable(pt, [HUB.x + 40, HUB.y + HUB.d - 4, HUB_H], [SLOT_C.x + SLOT_C.w - 16, SLOT_C.y + 10, 26], [[-40, -18, 34]], "blue", -0.5);
  agent(pt);
  hub(pt);
  instrument(pt, "ppk2", SLOT_A, { h: 24, screen: "52.3 mA", screenW: SLOT_A.w - 22, screenH: 26, label: "nordic ppk2", led: "green", knobs: [] });
  pcb(pt, "dut", DUT, { label: "DUT  3.3 V", led: "lit" });
  instrument(pt, "load", SLOT_C, { h: 30, screen: "0.9 Wh", screenW: SLOT_C.w - 20, screenH: 24, label: "soak log", led: "blue", knobs: [[SLOT_C.w - 24, 52, 6]] });
  instrument(pt, "dig", SLOT_B, { h: 22, screen: "D0..D7  in", screenTone: "lit", screenW: SLOT_B.w * 0.56, label: "digital in", led: "blue", knobs: [] });
  return pt.done();
}

const LABEL = `A hardware-in-the-loop bench drawn in isometric. An AI agent node on the left links to the HIL harness at the back, which fans out over colour-coded links to the instruments of the selected workbench and to the device under test at the front. Amber is the agent and its commands, blue is an instrument and its readings, green is a passing safety check.`;

const SCENES = [
  {
    id: "scpi",
    name: "SCPI bench",
    tag: "oscilloscope, SMU and supply over LAN, serial or GPIB",
    hint: "Hover a part to read it",
    readout: "hil ask keithley-2400 \":MEAS:VOLT?\"  ->  4.998 V",
    keys: [
      { mark: "lit", label: "the agent and the commands it sends" },
      { mark: "blue", label: "an instrument and its readings" },
      { mark: "green", label: "safety policy: pass" },
    ],
    parts: {
      agent: { label: "agent", detail: "Python, CLI or MCP -> hil.connect()" },
      hub: { label: "hil.instrument", detail: "write, query, safe_execute, capture" },
      smu: { label: "keithley-2400", detail: "SourceMeter (SMU), driver: scpi" },
      scope: { label: "rigol-dho1072", detail: "Oscilloscope, driver: scpi" },
      supply: { label: "ea-el9000b", detail: "DC power supply, driver: scpi" },
      dut: { label: "DUT", detail: "device under test" },
      bench: { label: "bench", detail: "tcp:// serial:// visa:// - one byte-pipe" },
    },
    svg: sceneSCPI(),
  },
  {
    id: "debug",
    name: "Flash and debug",
    tag: "build, flash, halt, step, RTT over SWD, JTAG or serial",
    hint: "Hover a part to read it",
    readout: "hil debug flash build/app.elf --chip nrf52840  ->  verified",
    keys: [
      { mark: "lit", label: "the agent and the firmware it flashes" },
      { mark: "blue", label: "a probe, a target and its wire" },
      { mark: "green", label: "verify + hash recorded" },
    ],
    parts: {
      agent: { label: "agent", detail: "build -> flash -> debug pipeline" },
      hub: { label: "hil.drivers.debug", detail: "probe-rs, pyOCD, OpenOCD, esptool, stlink" },
      probe: { label: "debug probe", detail: "debug://probe-rs, SWD or JTAG" },
      target: { label: "generic-cortex-m", detail: "nRF52840 target, flash + halt + step" },
      esp: { label: "esp32-devkit", detail: "serial ROM bootloader, no probe" },
      rtt: { label: "RTT + GDB", detail: "SEGGER RTT logs and a GDB session" },
      bench: { label: "bench", detail: "on-chip flash and debug, out of process" },
    },
    svg: sceneDebug(),
  },
  {
    id: "logic",
    name: "Logic analyzer",
    tag: "capture I2C, SPI or UART, decode to frames, spool the artifact",
    hint: "Hover a part to read it",
    readout: "hil atklogic decode cap.atkdl --decoder i2c  ->  128 frames",
    keys: [
      { mark: "lit", label: "the agent and the decoded frames" },
      { mark: "blue", label: "analyzer, probe bundle and channels" },
      { mark: "green", label: "capture written to the spool" },
    ],
    parts: {
      agent: { label: "agent", detail: "logic + decode steps in a procedure" },
      hub: { label: "hil.drivers.logic", detail: "in-process, no daemon, no GPL boundary" },
      la: { label: "alientek-atk-logic", detail: "logic://usb, 4 MS/s on channels 0,1" },
      dut: { label: "DUT", detail: "I2C bus under test (scl, sda)" },
      capture: { label: "cap.atkdl", detail: "CaptureArtifact: path + metadata, not bytes" },
      decode: { label: "decode", detail: "sigrok i2c -> 128 frames" },
      bench: { label: "bench", detail: "captures land in a TTL-managed spool" },
    },
    svg: sceneLogic(),
  },
  {
    id: "power",
    name: "Power profiling",
    tag: "source and measure current, log a soak, integrate energy",
    hint: "Hover a part to read it",
    readout: "hil log nordic-ppk2 --channel \":MEAS:CURR?\"  ->  52.3 mA",
    keys: [
      { mark: "lit", label: "the agent and the source setpoint" },
      { mark: "blue", label: "PPK2, the DUT and its digital inputs" },
      { mark: "green", label: "average draw within budget" },
    ],
    parts: {
      agent: { label: "agent", detail: "repeat + capture steps over a soak" },
      hub: { label: "hil.telemetry", detail: "Sample records -> summarize(), energy int(P dt)" },
      ppk2: { label: "nordic-ppk2", detail: "Power Profiler Kit II, driver: ppk2" },
      dut: { label: "DUT", detail: "powered at 3.3 V, current measured" },
      load: { label: "soak log", detail: "append-only NDJSON, 0.9 Wh integrated" },
      dig: { label: "digital in", detail: "8 input channels D0..D7 (inputs only)" },
      bench: { label: "bench", detail: "ppk2:// - pseudo-SCPI over ppk2-api" },
    },
    svg: scenePower(),
  },
].map((scene) => ({
  ...scene,
  svg: k.figureSvg({ width: WIDTH, height: HEIGHT, label: LABEL, body: scene.svg, className: "bench-svg" }),
}));

// The kit ships its own colour palette and a light theme; the page is dark-only
// and takes its palette from the Console tokens, so keep only the structural CSS
// (all var()-driven) plus the three non-colour knobs, and normalise the kit's
// spacing/tracking onto the Hallmark scale so sloplint's Floor holds.
const ISO_STRUCT =
  ':root{--anatomy-weight:.6px;--anatomy-ease:cubic-bezier(.32,.72,0,1);--anatomy-mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;}\n' +
  k.ISO_CSS.slice(k.ISO_CSS.indexOf(".iso-svg{"))
    .replaceAll("text-transform:uppercase;letter-spacing:.1em", "")
    .replace("padding:52px 24px", "padding:var(--space-xl) var(--space-lg)")
    .replace("gap:8px;margin-top:12px", "gap:var(--space-xs);margin-top:var(--space-sm)")
    .replace("gap:6px 18px", "gap:var(--space-xs) var(--space-md)")
    .replace("align-items:center;gap:7px", "align-items:center;gap:var(--space-xs)")
    .replace("padding:48px 12px 70px", "padding:var(--space-xl) var(--space-sm) var(--space-2xl)")
    .replace("line-height:1;letter-spacing:.04em", "line-height:1;letter-spacing:0");

writeFileSync(join(ASSETS, "iso.css"), ISO_STRUCT);
writeFileSync(join(ASSETS, "bench-scenes.js"), `window.__HIL_BENCH=${JSON.stringify(SCENES)};\n`);

// inline the default scene into index.html so the figure reads without JS
const INDEX = join(HERE, "..", "index.html");
if (existsSync(INDEX)) {
  const html = readFileSync(INDEX, "utf8");
  const next = html.replace(/(<div data-svg-host>)[\s\S]*?(<\/div>)/, (_m, a, b) => a + SCENES[0].svg + b);
  if (next !== html) writeFileSync(INDEX, next);
}

const kb = (s) => (Buffer.byteLength(s) / 1024).toFixed(1) + " KB";
console.log("wrote site/assets/iso.css", kb(ISO_STRUCT));
console.log("wrote site/assets/bench-scenes.js", kb(`window.__HIL_BENCH=${JSON.stringify(SCENES)};`));
for (const s of SCENES) console.log("  scene", s.id, kb(s.svg));

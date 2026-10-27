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

// the agent is a laptop: a thin keyboard deck plus a raked lid with a live screen
const AGENT = { x: -190, y: -126, w: 88, d: 62, r: 4 };
const BASE_H = 4;
const LID_TILT = 0.22;
const LID_H = 46;
const AGENT_H = BASE_H;
const HUB = { x: -70, y: -126, w: 152, d: 62, r: 6 };
const HUB_H = 30;

const SLOT_A = { x: 96, y: -122, w: 106, d: 68, r: 5 };   // right, back
const SLOT_B = { x: 78, y: 4, w: 120, d: 82, r: 6 };      // right, front
const SLOT_C = { x: -198, y: 0, w: 100, d: 80, r: 5 };    // left, front
const DUT = { x: -58, y: 38, w: 100, d: 68, r: 3 };       // front, centre
const POD = { x: -168, y: -40, w: 54, d: 38, r: 4 };      // debug probe pod, mid-left
const DISP = { x: -14, y: -40, w: 60, d: 44, r: 2 };      // i2c display panel, centre

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

function agent(pt, id = "agent", attrs = "") {
  // keyboard deck
  const g = [S(k.slabOf(AGENT, 0, BASE_H, P, 6, 0.6), { tone: "mid" })];
  const keys = [];
  for (let r = 0; r < 4; r++) for (let c = 0; c < 10; c++) keys.push(`<rect x="${7 + c * 8.2}" y="${8 + r * 6.4}" width="6.4" height="4.4" rx="0.8"/>`);
  keys.push(`<rect x="${AGENT.w / 2 - 11}" y="35" width="22" height="14" rx="1.4" fill="none" stroke-width="0.5"/>`);
  g.push(`<g transform="${k.topMatrix([AGENT.x, AGENT.y], BASE_H + 0.15, P, "x")}" style="fill:var(--anatomy-face);stroke:var(--anatomy-faint)">${keys.join("")}</g>`);
  // lid: a thin raked box hinged at the back edge of the deck
  const st = Math.sin(LID_TILT), ct = Math.cos(LID_TILT);
  const x0 = AGENT.x + 5, x1 = AGENT.x + AGENT.w - 5, yh = AGENT.y + 4;
  const u = [0, -st, ct], wv = [0, ct * 2.4, st * 2.4];
  const add = (p, q) => [p[0] + q[0], p[1] + q[1], p[2] + q[2]];
  const mul = (q, t) => [q[0] * t, q[1] * t, q[2] * t];
  const S1 = [x0, yh, BASE_H], S2 = [x1, yh, BASE_H];
  const S3 = add(S2, mul(u, LID_H)), S4 = add(S1, mul(u, LID_H));
  const B2 = add(S2, wv), B3 = add(S3, wv), B4 = add(S4, wv);
  const poly = (pts) => "M" + pts.map((p) => at(p).map((n) => n.toFixed(1)).join(" ")).join("L") + "Z";
  const seg = (a, b) => "M" + at(a).map((n) => n.toFixed(1)).join(" ") + "L" + at(b).map((n) => n.toFixed(1)).join(" ");
  g.push(`<path class="iso-fill" d="${poly([S1, S2, B2, B3, B4, S4])}"/>`);
  g.push(`<path class="iso-shade" data-shade="3" d="${poly([S1, S2, S3, S4])}"/>`);
  g.push(`<path class="iso-shade" data-shade="0" d="${poly([S2, B2, B3, S3])}"/>`);
  g.push(`<path class="iso-shade" data-shade="1" d="${poly([S4, S3, B3, B4])}"/>`);
  g.push(L(poly([S1, S2, B2, B3, B4, S4]) + seg(S2, S3) + seg(S4, S3), { tone: "mid" }));
  // screen glass: an agent CLI session in the lid's own plane, prompt chevron,
  // a task list with done checks, one spinning step, pending boxes, progress rail
  const ax = k.axisVector(P, [1, 0, 0]);
  const ay = k.axisVector(P, mul(u, -1));
  const o = at(S4);
  const M = `matrix(${ax[0].toFixed(3)} ${ax[1].toFixed(3)} ${ay[0].toFixed(3)} ${ay[1].toFixed(3)} ${o[0].toFixed(1)} ${o[1].toFixed(1)})`;
  const LW = x1 - x0;
  const GW = LW - 5, GH = LID_H - 5;
  const cli = [];
  cli.push(`<path d="M6 6.4l2.6 1.8-2.6 1.8" fill="none" stroke-width="1.1" style="stroke:var(--anatomy-lit)"/>`);
  cli.push(`<rect x="10.6" y="7.3" width="30" height="1.8" rx="0.6" style="fill:var(--anatomy-blue)"/>`);
  cli.push(`<rect class="agent-caret" x="42.2" y="7.3" width="2.2" height="1.8" style="fill:var(--anatomy-lit)"/>`);
  const tasks = [["done", 26], ["done", 34], ["run", 30], ["wait", 22], ["wait", 16]];
  tasks.forEach(([state, w], i) => {
    const y = 14 + i * 5.2;
    const box = state === "done"
      ? `<rect x="6" y="${(y - 1.7).toFixed(1)}" width="3.4" height="3.4" rx="0.8" fill="none" stroke-width="0.7" style="stroke:var(--anatomy-green)"/><path d="M6.9 ${(y - 0.2).toFixed(1)}l0.9 1 1.7 -2" fill="none" stroke-width="0.8" style="stroke:var(--anatomy-green)"/>`
      : state === "run"
        ? `<circle class="agent-spin" cx="7.7" cy="${y}" r="1.7" fill="none" stroke-width="0.9" stroke-dasharray="8 2.7" style="stroke:var(--anatomy-lit)"/>`
        : `<rect x="6" y="${(y - 1.7).toFixed(1)}" width="3.4" height="3.4" rx="0.8" fill="none" stroke-width="0.7" style="stroke:var(--anatomy-faint)"/>`;
    const tone = state === "run" ? "hi" : "mid";
    const op = state === "wait" ? ' opacity="0.55"' : "";
    cli.push(box + `<rect x="12" y="${(y - 0.9).toFixed(1)}" width="${w}" height="1.8" rx="0.6"${op} style="fill:var(--anatomy-${tone})"/>`);
  });
  cli.push(`<rect x="6" y="38.6" width="${GW - 12}" height="1.2" rx="0.6" opacity="0.35" style="fill:var(--anatomy-mid)"/>`);
  cli.push(`<rect x="6" y="38.6" width="${((GW - 12) * 0.55).toFixed(1)}" height="1.2" rx="0.6" style="fill:var(--anatomy-lit)"/>`);
  g.push(`<g transform="${M}"><rect x="2.5" y="2.5" width="${GW}" height="${GH}" rx="1.6" style="fill:var(--anatomy-page)"/>${cli.join("")}<rect x="2.5" y="2.5" width="${GW}" height="${GH}" rx="1.6" fill="none" stroke-width="0.5" style="stroke:var(--anatomy-lo)"/></g>`);
  g.push(D([at(add(add(S1, mul(u, LID_H - 7)), [LW - 7, 0, 0]))], { size: 1.1, tone: "lit", pulse: true }));
  pt.put(`<g data-part="${id}"${attrs}>${g.join("")}</g>`, centre(AGENT).concat(0));
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
  pt.put(`<g data-part="${id}"${o.attrs || ""}>${g.join("")}</g>`, [cx, cy, 0]);
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
  pt.put(`<g data-part="${id}"${o.attrs || ""}>${g.join("")}</g>`, [cx, cy, 0]);
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
  pt.put(`<g data-part="${id}"${o.attrs || ""}>${g.join("")}</g>`, [cx, cy, 0]);
}

// Cables: screen-space connectors with terminal dots and animated flow.
function cable(pt, fromWorld, toWorld, viaWorld, tone, bias = 0) {
  const svg = W(at(fromWorld), at(toWorld), { via: viaWorld.map((v) => at(v)), tone, flow: true, ends: "both", size: 1.5 });
  const mid = [(fromWorld[0] + toWorld[0]) / 2, (fromWorld[1] + toWorld[1]) / 2, (fromWorld[2] + toWorld[2]) / 2 + 6];
  pt.put(svg, mid, bias);
}

// A debug probe pod: small slab, SWD mark, attach LED.
function probePod(pt, id, plan, attrs = "") {
  const g = [S(k.slabOf(plan, 0, 10, P, 4, 0.8), { tone: "mid" })];
  g.push(L(k.planOutline(k.insetPlan(plan, 3), 10, P, 4), { tone: "faint" }));
  g.push(FT(k.topMatrix([centre(plan)[0], centre(plan)[1] + 1], 10.4, P, "x"), "swd", { size: 3.6, tone: "lit", anchor: "middle" }));
  g.push(D([at([plan.x + 6, plan.y + 6, 10.4])], { size: 1, tone: "green", pulse: true }));
  pt.put(`<g data-part="${id}"${attrs}>${g.join("")}</g>`, centre(plan).concat(0));
}

// An I2C OLED module: carrier pcb, glass panel, lit pixel cluster.
function display(pt, id, plan, o = {}) {
  const h = o.h || 4;
  const g = [S(k.slabOf(plan, 0, h, P, 3, 0.5), { tone: "mid" })];
  g.push(L(k.planOutline(k.insetPlan(plan, 2), h, P, 3), { tone: "faint" }));
  const glass = { x: plan.x + 6, y: plan.y + 5, w: plan.w - 12, d: plan.d - 14, r: 1 };
  g.push(S(k.slabOf(glass, h, 1.6, P, 2, 0.3), { tone: "lo" }));
  const px = [];
  for (let ix = 0; ix < 6; ix++) for (let iy = 0; iy < 3; iy++) {
    if ((ix + iy) % 2 === 0) px.push(at([glass.x + 5 + ix * ((glass.w - 10) / 5), glass.y + 4 + iy * ((glass.d - 8) / 2), h + 1.8]));
  }
  g.push(D(px, { size: 0.6, tone: "lit" }));
  if (o.label) g.push(FT(k.topMatrix([plan.x + plan.w / 2, plan.y + plan.d - 5], h + 0.3, P, "x"), o.label, { size: 3.2, tone: "mid", anchor: "middle" }));
  pt.put(`<g data-part="${id}"${o.attrs || ""}>${g.join("")}</g>`, centre(plan).concat(0));
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
  // debug link to the probe pod, then the SWD ribbon into the DUT
  cable(pt, [HUB.x + 24, HUB.y + HUB.d - 4, HUB_H], [POD.x + POD.w - 10, POD.y + 4, 10], [[-84, -66, 26]], "blue", -0.5);
  cable(pt, [POD.x + POD.w - 4, POD.y + POD.d - 8, 10], [DUT.x + 12, DUT.y + 6, 6], [[-88, 2, 16]], "blue", 0.2);
  // i2c tap from the DUT to the display panel, GPIO trigger back to the analyzer
  cable(pt, [DUT.x + 34, DUT.y + 2, 6], [DISP.x + 16, DISP.y + DISP.d - 2, 4], [[-12, 22, 10]], "blue", 0.3);
  cable(pt, [DUT.x + DUT.w - 4, DUT.y + 16, 6], [SLOT_A.x + 6, SLOT_A.y + SLOT_A.d - 8, 24], [[76, 0, 26]], "lit", 0.3);
  agent(pt);
  hub(pt);
  instrument(pt, "la", SLOT_A, { h: 26, screen: "ch0..2  4 MS/s", screenW: SLOT_A.w * 0.5, label: "atk-logic", led: "blue", knobs: [], buttons: 0 });
  // probe wire bundle from analyzer to the DUT
  pcb(pt, "dut", DUT, { label: "DUT  i2c", led: "lit" });
  probePod(pt, "probe", POD);
  display(pt, "disp", DISP, { label: "ssd1306" });
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

const LABEL = `A hardware-in-the-loop bench drawn in isometric. A laptop running your coding agent on the left links to the HIL harness at the back, which fans out over colour-coded links to the instruments of the selected workbench and to the device under test at the front. Amber is the agent and its commands, blue is an instrument and its readings, green is a passing safety check.`;

const LOOP_LABEL = `The firmware debug loop drawn as an isometric rotary index table. A central HIL harness hub drives five stations arranged on a ring: build, flash, debug, dump and measure. A small device-under-test token orbits the ring and stops at each station in turn, then returns to build to loop again. Amber is the harness drive and the loop, blue is a hardware wire, green is an artifact spooled as evidence.`;

// ---- the standalone debug-loop diagram: a rotary index table --------------
// Five stations sit on a ring around a central harness hub; a DUT token orbits
// the ring and bench.js lights each station as the token arrives, so the figure
// reads build -> flash -> debug -> dump -> measure -> loop on its own.

const TABLE_R = 186;
const TABLE_H = 16;
const RING_R = 128;
const STEP_DEG = [-90, -18, 54, 126, 198];
const STEP_META = [
  { id: "build", label: "build", detail: "fw/ compiles on the host, artifact hashed" },
  { id: "flash", label: "flash", detail: "probe-rs writes and verifies over SWD" },
  { id: "debug", label: "debug", detail: "halt at main, single-step, watch PC and faults" },
  { id: "dump", label: "dump", detail: "RTT console and memory image spooled as artifacts" },
  { id: "measure", label: "measure", detail: "bench procedure fires instruments, gated on the DUT" },
];

function sceneLoop() {
  const pt = painter();
  const rad = (d) => (d * Math.PI) / 180;
  const ringPt = (deg, r, z) => [r * Math.cos(rad(deg)), r * Math.sin(rad(deg)), z];
  const circle = (r, z) => {
    let d = "";
    for (let i = 0; i <= 96; i++) {
      const p = at(ringPt((i / 96) * 360, r, z));
      d += (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2);
    }
    return d + "Z";
  };
  // round rotary table as the ground plane
  pt.put(
    `<g data-part="table">${S(k.cylinder(0, 0, TABLE_R, -TABLE_H, TABLE_H, P, 72), { tone: "mid" })}${L(circle(TABLE_R - 10, 0.2), { tone: "faint" })}${L(circle(46, 0.2), { tone: "faint" })}</g>`,
    [0, 120, -TABLE_H],
    -1e6,
  );
  // the loop track the token rides
  pt.put(
    `<g aria-hidden="true">${L(circle(RING_R + 8, 0.3), { tone: "faint" })}${L(circle(RING_R - 8, 0.3), { tone: "faint" })}${L(circle(RING_R, 0.35), { tone: "lo" })}</g>`,
    [0, 60, 0],
    -0.7,
  );
  // radial drives: the hub reaches every station
  STEP_DEG.forEach((deg, i) => {
    const [ix, iy] = ringPt(deg, 36, 20);
    const [ox, oy] = ringPt(deg, RING_R - 22, 12);
    const [mx, my] = ringPt(deg, (36 + RING_R - 22) / 2, 26);
    pt.put(
      `<g data-edge="${i}">${W(at([ix, iy, 20]), at([ox, oy, 12]), { via: [at([mx, my, 26])], tone: "lit", flow: true, ends: "both", size: 1.5 })}</g>`,
      [mx, my, 20],
      -0.4,
    );
  });
  // stations around the ring
  STEP_DEG.forEach((deg, i) => {
    const [cx, cy] = ringPt(deg, RING_R, 0);
    const plan = { x: cx - 23, y: cy - 18, w: 46, d: 36, r: 3 };
    const attrs = ` data-step="${i}" data-label="${STEP_META[i].label}" data-detail="${STEP_META[i].detail}"`;
    if (i === 1) probePod(pt, STEP_META[i].id, plan, attrs);
    else if (i === 3) artifact(pt, STEP_META[i].id, plan, { h: 10, label: "rtt+mem", led: "green", attrs });
    else if (i === 0) instrument(pt, STEP_META[i].id, plan, { h: 22, screen: "make fw", screenW: plan.w - 14, screenH: 14, label: "build", led: "lit", knobs: [], attrs });
    else if (i === 2) instrument(pt, STEP_META[i].id, plan, { h: 22, screen: "PC 0x2A18", screenTone: "lit", screenW: plan.w - 14, screenH: 14, label: "halt+step", led: "blue", knobs: [], attrs });
    else instrument(pt, STEP_META[i].id, plan, { h: 22, screen: "3.298 V", screenW: plan.w - 14, screenH: 14, label: "measure", led: "lit", knobs: [], attrs });
  });
  // central harness hub
  pt.put(
    `<g data-part="hub" data-label="hil harness" data-detail="one harness drives every step of the loop">${S(k.cylinder(0, 0, 32, 0, 22, P, 44), { tone: "mid" })}${FT(k.topMatrix([0, 0], 22.4, P, "x"), "hil", { size: 6, tone: "lit", anchor: "middle" })}</g>`,
    [0, 0, 22],
  );
  // the orbiting DUT token (bench.js translates it around the ring)
  const [tx, ty] = ringPt(STEP_DEG[0], RING_R, 0);
  const tplan = { x: tx - 11, y: ty - 8, w: 22, d: 16, r: 2 };
  const OC = at([0, 0, 0.6]);
  const OX = at([1, 0, 0.6]);
  const OY = at([0, 1, 0.6]);
  const tokenAttrs =
    ` data-ocx="${OC[0].toFixed(2)}" data-ocy="${OC[1].toFixed(2)}"` +
    ` data-oax="${(OX[0] - OC[0]).toFixed(4)}" data-oay="${(OX[1] - OC[1]).toFixed(4)}"` +
    ` data-obx="${(OY[0] - OC[0]).toFixed(4)}" data-oby="${(OY[1] - OC[1]).toFixed(4)}"` +
    ` data-or="${RING_R}" data-a0="${STEP_DEG[0]}"`;
  pt.put(
    `<g class="loop-token"${tokenAttrs}>${S(k.slabOf(tplan, 0.6, 5, P, 2, 0.4), { tone: "hi" })}${D([at([tx, ty, 5.4])], { size: 1.2, tone: "lit", pulse: true })}</g>`,
    [tx, ty, 4],
    1,
  );
  // numbered labels outside the ring
  const tags = STEP_DEG.map((deg, i) => {
    const [lx, ly] = ringPt(deg, RING_R + 34, 0.3);
    return FT(k.topMatrix([lx, ly], 0.3, P, "x"), `${i + 1} ${STEP_META[i].label}`, { size: 4.6, tone: "mid", anchor: "middle" });
  }).join("");
  pt.put(`<g aria-hidden="true">${tags}</g>`, [0, 60, 0], -0.6);
  return pt.done();
}

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
    prompts: [
      { tool: "hil_exec", text: "Set the Keithley 2400 to source 3.3 V with a 100 mA limit, then ask the Rigol scope whether the rail settles within 10 mV before the DUT powers up. Use hil_exec so the safety policy sees every command." },
      { tool: "hil_exec, hil_errors", text: "Sweep the EA supply from 0 to 5 V in 100 mV steps, read back at each step, drain the error queue with hil_errors, and plot the linearity error from the returned JSON." },
    ],
    session: [
      ["you", "Set the Keithley 2400 to 3.3 V with a 100 mA limit, then verify the rail on the scope."],
      ["call", "hil_devices keithley-2400"],
      ["ok", "profile loaded: SMU, driver scpi, safety policy pass"],
      ["call", "hil_exec SOUR:VOLT 3.3, CURR:LIM 0.1"],
      ["ok", "ok, error queue empty"],
      ["call", "hil_exec MEAS:VOLT? on rigol-dho1072"],
      ["ok", "3.298 V, within 10 mV: DUT clear to power"],
    ],
    parts: {
      agent: { label: "coding agent", detail: "Claude Code, Codex, Cursor or Qoder over MCP" },
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
    prompts: [
      { tool: "hil_debug", text: "Build the firmware in fw/, flash it to the nRF52840 DK with hil_debug, then halt at main and single-step through init until the status LED blinks. Report the program counter and any hard fault." },
      { tool: "hil_debug", text: "Once flashing verifies, open an RTT console with hil_debug and watch ten seconds of log for assertions before calling the run good." },
    ],
    session: [
      ["you", "Flash fw/app.elf to the nRF52840 DK, step through init, and watch for faults."],
      ["call", "hil_debug_probe"],
      ["ok", "1 probe: J-Link, serial 000687745590"],
      ["call", "hil_debug flash build/app.elf"],
      ["ok", "verified, hash 9c41e2 recorded"],
      ["call", "hil_debug halt, step 4, rtt 10 s"],
      ["ok", "PC 0x00002A18, no hard fault, log clean"],
    ],
    parts: {
      agent: { label: "coding agent", detail: "builds, flashes and steps through main" },
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
    tag: "flash the driver over SWD, trigger on GPIO, decode I2C to frames",
    hint: "Hover a part to read it",
    readout: "hil atklogic decode cap.atkdl --decoder i2c  ->  128 frames to 0x3c",
    keys: [
      { mark: "lit", label: "the agent, the GPIO trigger and live frames" },
      { mark: "blue", label: "analyzer, probe bundle, SWD ribbon and I2C taps" },
      { mark: "green", label: "capture written to the spool" },
    ],
    prompts: [
      { tool: "hil_debug, hil_capture", text: "Flash the SSD1306 display driver to the DUT with hil_debug over SWD, then capture two seconds of SCL, SDA and the GPIO0 trigger at 4 MS/s and decode every frame." },
      { tool: "hil_capture", text: "Gate the capture on the DUT trigger edge, then check the decoded init sequence against the panel datasheet: address, control bytes, and any frame that NACKs." },
    ],
    session: [
      ["you", "Flash the display driver, capture I2C gated on the GPIO trigger, and validate the init sequence."],
      ["call", "hil_debug flash build/display-driver.elf"],
      ["ok", "verified, SWD at 4 MHz"],
      ["call", "hil_capture logic://usb, ch0 scl, ch1 sda, ch2 trig, 2 s"],
      ["ok", "8.0 M samples, 14 trigger edges"],
      ["call", "hil atklogic decode cap.atkdl, decoder i2c"],
      ["ok", "128 frames to 0x3c, init matches the datasheet"],
      ["note", "trigger to first SCL edge 412 us, stable across runs"],
    ],
    parts: {
      agent: { label: "coding agent", detail: "reads decoded frames as JSON" },
      hub: { label: "hil.drivers.logic", detail: "in-process, no daemon, no GPL boundary" },
      la: { label: "alientek-atk-logic", detail: "logic://usb, 4 MS/s on ch0..ch2, trigger on ch2" },
      probe: { label: "debug probe", detail: "probe-rs over SWD, flashes the driver" },
      dut: { label: "DUT", detail: "I2C master, GPIO0 pulses once per frame" },
      disp: { label: "i2c display", detail: "SSD1306 panel at 0x3c, bus tapped" },
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
    prompts: [
      { tool: "hil_capture", text: "Build a power bench with the PPK2 as the 3V3 source for the DUT. Stream one hour of current with hil_capture into the spool, then report mean, p95 and total energy in Wh." },
      { tool: "hil_debug, hil_capture", text: "Design and flash a low-power firmware build, re-run the same one-hour profile, and score it against the baseline capture in mean current and estimated battery days." },
    ],
    session: [
      ["you", "Source 3V3 from the PPK2 and log one hour of current on the real device."],
      ["call", "hil_setup ppk2"],
      ["ok", "backend ready, ppk2-api 0.9.2"],
      ["call", "hil_capture ppk2:// MEAS:CURR? for 3600 s"],
      ["ok", "NDJSON spool written, 3.6 M samples"],
      ["call", "hil telemetry summarize"],
      ["ok", "mean 52.3 mA, p95 81.0 mA, 0.9 Wh"],
      ["note", "baseline saved, ready to A/B the firmware"],
    ],
    parts: {
      agent: { label: "coding agent", detail: "plans the soak, scores the firmware" },
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
const WHO = { you: "you", call: "hil", ok: "ok", note: "note" };
const sessionItems = (s) => (s.session || [])
  .map(([kind, text], i) => `<li class="console-line" data-kind="${kind}" style="--i:${i}"><span class="console-who">${WHO[kind]}</span><p>${text}</p></li>`)
  .join("");
const promptItems = (s) => (s.prompts || [])
  .map((p) => `<li class="prompt-item"><span class="prompt-tool">${p.tool}</span><p>${p.text}</p><button class="copy-btn" type="button" data-copy="${p.text.replace(/"/g, "&quot;")}">copy</button></li>`)
  .join("");
if (existsSync(INDEX)) {
  const html = readFileSync(INDEX, "utf8");
  let next = html.replace(/(<div data-svg-host>)[\s\S]*?(<\/div>)/, (_m, a, b) => a + SCENES[0].svg + b);
  next = next.replace(/(<ul class="prompt-list" data-prompts-host>)[\s\S]*?(<\/ul>)/, (_m, a, b) => a + promptItems(SCENES[0]) + b);
  next = next.replace(/(<ol class="console-lines" data-console-host>)[\s\S]*?(<\/ol>)/, (_m, a, b) => a + sessionItems(SCENES[0]) + b);
  next = next.replace(/(<div data-loop-svg>)[\s\S]*?(<\/div>)/, (_m, a, b) => a + k.figureSvg({
    width: WIDTH,
    height: HEIGHT,
    label: LOOP_LABEL,
    body: sceneLoop(),
    className: "loop-svg",
  }) + b);
  if (next !== html) writeFileSync(INDEX, next);
}

const kb = (s) => (Buffer.byteLength(s) / 1024).toFixed(1) + " KB";
console.log("wrote site/assets/iso.css", kb(ISO_STRUCT));
console.log("wrote site/assets/bench-scenes.js", kb(`window.__HIL_BENCH=${JSON.stringify(SCENES)};`));
for (const s of SCENES) console.log("  scene", s.id, kb(s.svg));

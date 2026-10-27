// HIL bench switcher: renders the four Anatomy workbench scenes and wires
// tab/arrow-key switching plus hover/focus part readouts. Degrades to the
// static default scene already inlined in index.html when JS is off.
(function () {
  var scenes = window.__HIL_BENCH;
  if (!scenes || !scenes.length) return;

  var root = document.getElementById("bench");
  if (!root) return;

  var I18N = window.__HIL_I18N || { t: function (s) { return s; }, zh: function () { return false; } };

  var tabsEl = root.querySelector(".bench-tabs");
  var plateEl = root.querySelector(".iso-plate");
  var svgHost = root.querySelector("[data-svg-host]");
  var figCorner = root.querySelector('[data-corner="fig"]');
  var titleCorner = root.querySelector('[data-corner="title"]');
  var hintCorner = root.querySelector('[data-corner="hint"]');
  var readoutCorner = root.querySelector("[data-readout]");
  var legendEl = root.querySelector(".iso-legend .iso-keys");
  var metaTag = root.querySelector(".bench-meta .tag");
  var partEl = root.querySelector(".bench-part");
  var promptsHost = root.querySelector("[data-prompts-host]");
  var consoleEl = root.querySelector("[data-console]");
  var consoleHost = root.querySelector("[data-console-host]");
  var consoleTag = root.querySelector("[data-console-tag]");
  var WHO = { you: "you", call: "hil", ok: "ok", note: "note" };

  var current = 0;

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function swatch(mark) {
    if (mark === "raised" || mark === "flat" || mark === "lit") {
      var body = mark === "flat" ? "M2 6.5 8 3.5 14 6.5 8 9.5Z" : "M2 4.5 8 1.5 14 4.5 14 6 8 9 2 6Z";
      var crease = mark === "flat" ? "" : '<path class="iso-swatch-crease" d="M2 4.5 8 7.5 14 4.5"/>';
      return '<svg class="iso-swatch" data-mark="' + mark + '" viewBox="0 0 16 10" aria-hidden="true"><path d="' + body + '"/>' + crease + "</svg>";
    }
    return '<svg class="iso-swatch" data-mark="' + mark + '" viewBox="0 0 16 10" aria-hidden="true"><path d="M1.5 5h13"/></svg>';
  }

  function buildTabs() {
    tabsEl.setAttribute("role", "tablist");
    tabsEl.setAttribute("aria-label", I18N.t("Workbenches"));
    scenes.forEach(function (s, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "bench-tab";
      b.id = "bench-tab-" + s.id;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", "bench-figure");
      b.textContent = I18N.t(s.name);
      b.addEventListener("click", function () { select(i); });
      tabsEl.appendChild(b);
    });
    tabsEl.addEventListener("keydown", function (e) {
      var n = scenes.length;
      if (e.key === "ArrowRight") { e.preventDefault(); select((current + 1) % n, true); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); select((current - 1 + n) % n, true); }
      else if (e.key === "Home") { e.preventDefault(); select(0, true); }
      else if (e.key === "End") { e.preventDefault(); select(n - 1, true); }
    });
  }

  function bindParts() {
    var svg = svgHost.querySelector("svg");
    if (!svg) return;
    svg.setAttribute("tabindex", "0");
    var parts = svg.querySelectorAll("[data-part]");
    parts.forEach(function (g) {
      var id = g.getAttribute("data-part");
      function show() { highlight(svg, id); readPart(id); }
      function hide() { highlight(svg, null); readPart(null); }
      g.addEventListener("pointerenter", show);
      g.addEventListener("pointerleave", hide);
      g.addEventListener("focusin", show);
      g.addEventListener("focusout", hide);
      g.addEventListener("click", show);
    });
  }

  function highlight(svg, id) {
    svg.querySelectorAll("[data-part]").forEach(function (g) {
      g.classList.toggle("is-dim", !!id && g.getAttribute("data-part") !== id);
      g.classList.toggle("is-hot", !!id && g.getAttribute("data-part") === id);
    });
  }

  function readPart(id) {
    var s = scenes[current];
    if (!id || !s.parts || !s.parts[id]) {
      partEl.textContent = I18N.t(s.hint) || "";
      readoutCorner.textContent = I18N.t(s.readout) || "";
      return;
    }
    var p = s.parts[id];
    partEl.textContent = I18N.t(p.label) + ", " + I18N.t(p.detail);
    readoutCorner.textContent = I18N.t(p.label);
  }

  function select(i, focus) {
    current = i;
    var s = scenes[i];
    tabsEl.querySelectorAll(".bench-tab").forEach(function (b, bi) {
      b.setAttribute("aria-selected", bi === i ? "true" : "false");
      b.tabIndex = bi === i ? 0 : -1;
      if (bi === i && focus) b.focus();
    });
    svgHost.innerHTML = s.svg;
    if (figCorner) figCorner.textContent = I18N.t("fig ") + (i + 1) + " / " + scenes.length;
    if (titleCorner) titleCorner.textContent = I18N.t(s.name);
    if (hintCorner) hintCorner.textContent = I18N.t(s.hint) || "";
    if (readoutCorner) readoutCorner.textContent = I18N.t(s.readout) || "";
    if (legendEl) {
      legendEl.innerHTML = (s.keys || []).map(function (k) {
        return '<span class="iso-key">' + swatch(k.mark) + esc(I18N.t(k.label)) + "</span>";
      }).join("");
    }
    if (metaTag) metaTag.textContent = I18N.t(s.tag) || "";
    if (promptsHost) {
      promptsHost.innerHTML = (s.prompts || []).map(function (p) {
        var txt = I18N.t(p.text);
        return '<li class="prompt-item"><span class="prompt-tool">' + esc(p.tool) + "</span><p>" + esc(txt) + '</p><button class="copy-btn" type="button" data-copy="' + esc(txt) + '">' + I18N.t("copy") + "</button></li>";
      }).join("");
    }
    renderSession(s);
    plateEl.setAttribute("aria-label", I18N.t(s.name + " workbench"));
    bindParts();
    readPart(null);
  }

  // agent session transcript: re-render the lines, then replay the reveal
  var seen = false;
  function renderSession(s) {
    if (!consoleHost) return;
    consoleHost.innerHTML = (s.session || []).map(function (line, n) {
      return '<li class="console-line" data-kind="' + esc(line[0]) + '" style="--i:' + n + '"><span class="console-who">' + (WHO[line[0]] || "") + "</span><p>" + esc(I18N.t(line[1])) + "</p></li>";
    }).join("");
    if (consoleTag) consoleTag.textContent = s.id;
    if (seen && consoleEl) {
      consoleEl.classList.remove("is-live");
      void consoleEl.offsetWidth;
      consoleEl.classList.add("is-live");
    }
  }
  if (consoleEl) {
    consoleEl.classList.add("js");
    if ("IntersectionObserver" in window) {
      var consoleIO = new IntersectionObserver(function (entries) {
        if (!entries[0].isIntersecting) return;
        seen = true;
        consoleIO.disconnect();
        consoleEl.classList.add("is-live");
      }, { threshold: 0.25 });
      consoleIO.observe(consoleEl);
    } else {
      seen = true;
      consoleEl.classList.add("is-live");
    }
  }

  buildTabs();
  bindParts();
  // sync tab state with the inlined default scene (index 0)
  tabsEl.querySelectorAll(".bench-tab").forEach(function (b, bi) {
    b.setAttribute("aria-selected", bi === 0 ? "true" : "false");
    b.tabIndex = bi === 0 ? 0 : -1;
  });
  readPart(null);

  // the default scene is inlined as English; when the page loads in Chinese,
  // re-render scene 0 so its corners, legend, prompts and session translate too
  if (I18N.zh()) select(0);
  // re-render the active scene whenever the language toggle fires
  document.addEventListener("hil:lang", function () {
    tabsEl.setAttribute("aria-label", I18N.t("Workbenches"));
    tabsEl.querySelectorAll(".bench-tab").forEach(function (b, bi) {
      b.textContent = I18N.t(scenes[bi].name);
    });
    select(current);
  });

  // copy buttons: delegated, so re-rendered bench prompts keep working
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-copy]") : null;
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    var done = function () {
      btn.setAttribute("data-copied", "");
      btn.textContent = I18N.t("copied");
      setTimeout(function () { btn.removeAttribute("data-copied"); btn.textContent = I18N.t("copy"); }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, done);
    } else {
      var ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e2) {}
      document.body.removeChild(ta); done();
    }
  });
})();

// Debug-loop figure: a rotary index table. bench.js orbits the DUT token around
// the ring and lights each station (plus its step card and drive edge) as the
// token arrives, so the loop animates on its own. Hover/focus a station to pin
// it and read its step; leave to resume. Reduced motion parks at step 1.
(function () {
  var host = document.querySelector("[data-loop-svg]");
  if (!host) return;
  var svg = host.querySelector("svg");
  if (!svg) return;
  var steps = [].slice.call(svg.querySelectorAll("[data-step]")).sort(function (a, b) {
    return +a.getAttribute("data-step") - +b.getAttribute("data-step");
  });
  if (!steps.length) return;
  var edges = [].slice.call(svg.querySelectorAll("[data-edge]"));
  var items = [].slice.call(document.querySelectorAll(".loop-steps li"));
  var hub = svg.querySelector('[data-part="hub"]');
  var token = svg.querySelector(".loop-token");
  var readout = document.querySelector("[data-loop-readout]");
  var fallback = readout ? readout.textContent : "";
  svg.setAttribute("tabindex", "0");

  var ocx = +token.getAttribute("data-ocx"), ocy = +token.getAttribute("data-ocy");
  var oax = +token.getAttribute("data-oax"), oay = +token.getAttribute("data-oay");
  var obx = +token.getAttribute("data-obx"), oby = +token.getAttribute("data-oby");
  var OR = +token.getAttribute("data-or"), A0 = +token.getAttribute("data-a0");
  var SEG = 360 / steps.length;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var active = -1, paused = false, angle = A0, last = null;

  function point(deg) {
    var t = (deg * Math.PI) / 180, c = Math.cos(t) * OR, s = Math.sin(t) * OR;
    return [ocx + c * oax + s * obx, ocy + c * oay + s * oby];
  }
  function place(deg) {
    var p = point(deg), p0 = point(A0);
    token.setAttribute("transform", "translate(" + (p[0] - p0[0]).toFixed(2) + " " + (p[1] - p0[1]).toFixed(2) + ")");
  }
  function setActive(i) {
    if (i === active) return;
    active = i;
    steps.forEach(function (g, gi) {
      g.classList.toggle("is-hot", gi === i);
      g.classList.toggle("is-dim", gi !== i);
    });
    edges.forEach(function (e, ei) { e.classList.toggle("is-live", ei === i); });
    items.forEach(function (li, lii) { li.classList.toggle("is-active", lii === i); });
    if (readout) readout.textContent = steps[i].getAttribute("data-label") + ": " + steps[i].getAttribute("data-detail");
  }
  function readHub() {
    if (readout && hub) readout.textContent = hub.getAttribute("data-label") + ": " + hub.getAttribute("data-detail");
  }

  steps.forEach(function (g) {
    var i = +g.getAttribute("data-step");
    function pin() { paused = true; setActive(i); }
    function unpin() { paused = false; if (reduce) setActive(0); }
    g.addEventListener("pointerenter", pin);
    g.addEventListener("focusin", pin);
    g.addEventListener("click", pin);
    g.addEventListener("pointerleave", unpin);
    g.addEventListener("focusout", unpin);
  });
  if (hub) {
    hub.addEventListener("pointerenter", function () { paused = true; readHub(); });
    hub.addEventListener("focusin", function () { paused = true; readHub(); });
    hub.addEventListener("pointerleave", function () { paused = false; });
    hub.addEventListener("focusout", function () { paused = false; });
  }

  place(A0);
  setActive(0);
  if (reduce) return; // park at step 1; hover still pins

  function frame(ts) {
    if (last == null) last = ts;
    var dt = Math.min(64, ts - last);
    last = ts;
    if (!paused && !document.hidden) {
      angle += dt * 0.036; // ~10 s per revolution, ~2 s per station
      place(angle);
      setActive(Math.floor((((angle - A0) % 360) + 360) % 360 / SEG) % steps.length);
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();

// HIL bench switcher: renders the four Anatomy workbench scenes and wires
// tab/arrow-key switching plus hover/focus part readouts. Degrades to the
// static default scene already inlined in index.html when JS is off.
(function () {
  var scenes = window.__HIL_BENCH;
  if (!scenes || !scenes.length) return;

  var root = document.getElementById("bench");
  if (!root) return;

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
    tabsEl.setAttribute("aria-label", "Workbenches");
    scenes.forEach(function (s, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "bench-tab";
      b.id = "bench-tab-" + s.id;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", "bench-figure");
      b.textContent = s.name;
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
      partEl.textContent = s.hint || "";
      readoutCorner.textContent = s.readout || "";
      return;
    }
    var p = s.parts[id];
    partEl.textContent = p.label + " — " + p.detail;
    readoutCorner.textContent = p.label;
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
    if (figCorner) figCorner.textContent = "fig " + (i + 1) + " / " + scenes.length;
    if (titleCorner) titleCorner.textContent = s.name;
    if (hintCorner) hintCorner.textContent = s.hint || "";
    if (readoutCorner) readoutCorner.textContent = s.readout || "";
    if (legendEl) {
      legendEl.innerHTML = (s.keys || []).map(function (k) {
        return '<span class="iso-key">' + swatch(k.mark) + esc(k.label) + "</span>";
      }).join("");
    }
    if (metaTag) metaTag.textContent = s.tag || "";
    plateEl.setAttribute("aria-label", s.name + " workbench");
    bindParts();
    readPart(null);
  }

  buildTabs();
  bindParts();
  // sync tab state with the inlined default scene (index 0)
  tabsEl.querySelectorAll(".bench-tab").forEach(function (b, bi) {
    b.setAttribute("aria-selected", bi === 0 ? "true" : "false");
    b.tabIndex = bi === 0 ? 0 : -1;
  });
  readPart(null);

  // copy buttons (install prompts)
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      var done = function () {
        btn.setAttribute("data-copied", "");
        btn.textContent = "copied";
        setTimeout(function () { btn.removeAttribute("data-copied"); btn.textContent = "copy"; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta); done();
      }
    });
  });
})();

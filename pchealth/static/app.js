"use strict";
// PC Health dashboard. Read-only: it only ever issues GET requests.
// Every string from the server is inserted as text (textContent), never as HTML.
(() => {
  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const GB = 2 ** 30, MB = 2 ** 20;
  const POLL_MS = 2000;
  const HISTORY_MS = { "1h": 10000, "24h": 60000, "7d": 300000 };
  const ICONS = {
    ok: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1.2 14.4-4.2-4.2 1.5-1.5 2.7 2.7 5.7-5.7 1.5 1.5-7.2 7.2z",
    warn: "M12 2.5 1 21h22L12 2.5zM11 9h2v6h-2V9zm0 8h2v2h-2v-2z",
    crit: "M8.3 2h7.4L22 8.3v7.4L15.7 22H8.3L2 15.7V8.3L8.3 2zM11 7v7h2V7h-2zm0 9v2h2v-2h-2z",
    info: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1 5h2v2h-2V7zm0 4h2v6h-2v-6z",
  };
  const LEVEL_WORD = { ok: "Healthy", warn: "Warning", crit: "Critical", info: "Note" };
  const STATE_WORD = { working: "Working", waiting: "Waiting for you", idle: "Idle", unknown: "Running" };
  const RANK = { ok: 0, info: 0, warn: 1, crit: 2 };

  // Two looks, chosen per device in Settings (look.js has already applied the choice):
  // "simple", the default, is flat and still; "glass" adds the backdrop light, frosted panels and glass bars.
  const root = document.documentElement;
  const glassLook = () => root.dataset.look === "glass";

  // Glass that bends light is an SVG filter used as a backdrop filter. Only Chromium draws that; other
  // browsers accept the rule and then show no glass at all, so it is switched on by browser name.
  // The bending is redone on every frame the page draws, which is real work without a graphics card, so it is
  // only on while the page is being scrolled: it is there as content moves under the glass, and relaxes away
  // once the page comes to rest. A page left open, wherever it was scrolled to, pays nothing for it.
  const chromium = /\bChrom(?:e|ium)\/(\d+)/.exec(navigator.userAgent);
  const lens = document.querySelector("#lg feDisplacementMap");
  if (chromium && Number(chromium[1]) >= 129 && lens) {
    const FULL = Number(lens.getAttribute("scale"));
    let strength = FULL, rest = 0, frame = 0;
    const bend = (v) => { strength = v; lens.setAttribute("scale", v.toFixed(1)); };
    const settle = () => {
      root.classList.remove("moving");
      bend(FULL);
    };
    const relax = () => {
      if (matchMedia("(prefers-reduced-motion: reduce)").matches) return settle();
      const t0 = performance.now(), from = strength;
      const step = (now) => {
        const k = Math.min(1, (now - t0) / 360);
        bend(from * (1 - k) * (1 - k));
        if (k < 1) frame = requestAnimationFrame(step);
        else { frame = 0; settle(); }
      };
      frame = requestAnimationFrame(step);
    };
    root.classList.add("refract");
    addEventListener("scroll", () => {
      if (!glassLook()) return;
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      if (strength !== FULL) bend(FULL);
      root.classList.add("moving");
      clearTimeout(rest);
      rest = setTimeout(relax, 240);
    }, { passive: true });
  }

  // ---------------------------------------------------------------- small helpers

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function setText(e, t) {
    t = t == null ? "" : String(t);
    if (e.textContent !== t) e.textContent = t;
  }
  function setClass(e, c) { if (e.className !== c) e.className = c; }
  /** A bar's fill. A move worth seeing eases (the transition is in app.css); a small one just lands.
   *  An idle machine jitters by a point or two all day, and easing each of those would keep the page
   *  drawing frames for nothing. */
  function setWidth(e, pct) {
    const v = Math.max(0, Math.min(100, pct || 0));
    const w = v.toFixed(1) + "%";
    if (e.style.width === w) return;
    const eased = e._pct == null || Math.abs(v - e._pct) >= 3;
    const duration = eased ? "" : "0s";
    if (e.style.transitionDuration !== duration) e.style.transitionDuration = duration;
    e._pct = v;
    e.style.width = w;
  }
  function icon(level) {
    const s = document.createElementNS(SVG_NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("class", "icon lv-" + level);
    s.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", ICONS[level] || ICONS.info);
    p.setAttribute("fill", "currentColor");
    s.appendChild(p);
    return s;
  }
  function setIcon(slot, level) {
    if (slot.dataset.level === level) return;
    slot.dataset.level = level;
    slot.replaceChildren(icon(level));
  }
  function chip(level, text) {
    const c = el("span", "chip lv-" + level);
    c.append(icon(level), document.createTextNode(text || LEVEL_WORD[level]));
    return c;
  }
  function bytes(b) {
    if (b == null) return "–";
    const g = b / GB;
    if (g >= 1000) return (g / 1024).toFixed(1).replace(/\.0$/, "") + " TB";
    if (g >= 100) return Math.round(g) + " GB";
    if (g >= 1) return g.toFixed(1).replace(/\.0$/, "") + " GB";
    const m = b / MB;
    return (m >= 10 ? Math.round(m) : m.toFixed(1).replace(/\.0$/, "")) + " MB";
  }
  function signedBytes(b) { return (b < 0 ? "−" : "+") + bytes(Math.abs(b)); }
  function dur(s) {
    s = Math.max(0, Math.round(s));
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    if (s < 36 * 3600) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " days";
  }
  function longDur(s) {
    s = Math.max(0, Math.round(s));
    const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
    if (s < 5400) return plural(Math.max(1, Math.round(s / 60)), "minute");
    if (s < 36 * 3600) return plural(Math.round(s / 3600), "hour");
    return plural(Math.round(s / 86400), "day");
  }
  function cores(c) { return (c >= 10 ? c.toFixed(0) : c.toFixed(1)) + (c === 1 ? " core" : " cores"); }
  function rate(bps) {
    if (!bps || bps < 100 * 1024) return "–";
    const m = bps / MB;
    return (m >= 10 ? Math.round(m) : m.toFixed(1)) + " MB/s";
  }
  const pad = (n) => String(n).padStart(2, "0");
  function clock(ts, secs) {
    const d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + (secs ? ":" + pad(d.getSeconds()) : "");
  }
  function when(ts) {
    const d = new Date(ts * 1000), n = new Date();
    if (d.toDateString() === n.toDateString()) return "Today " + clock(ts);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + clock(ts);
  }
  const whenIn = (ts) => when(ts).replace(/^Today/, "today");       // inside a sentence

  let settled = false;                      // true once the first snapshot is on screen
  /** Reorder/create/remove children of `parent` so they match `items`, keeping existing nodes. */
  function sync(parent, items, key, make, update) {
    const have = new Map();
    for (const node of parent.children) have.set(node.dataset.key, node);
    let prev = null;
    for (const item of items) {
      const k = String(key(item));
      let node = have.get(k);
      const fresh = !node;
      if (node) have.delete(k); else { node = make(item); node.dataset.key = k; }
      update(node, item);
      if (fresh && settled && "enter" in parent.dataset) node.classList.add("enter");     // lists marked data-enter
      const want = prev ? prev.nextSibling : parent.firstChild;
      if (node !== want) parent.insertBefore(node, want);
      prev = node;
    }
    for (const node of have.values()) node.remove();
  }

  function copyText(text, button) {
    const done = () => {                      // the button turns into a green tick for a moment
      button._label.textContent = "Copied";
      button.classList.add("copied");
      clearTimeout(button._timer);
      button._timer = setTimeout(() => { button._label.textContent = "Copy"; button.classList.remove("copied"); }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
    } else fallbackCopy(text, done);
  }
  function fallbackCopy(text, done) {      // plain http on the home network has no clipboard API
    const ta = el("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { if (document.execCommand("copy")) done(); } catch (e) { /* the command stays selectable */ }
    ta.remove();
  }
  function commandBox(cmd) {
    const box = el("div", "cmd");
    const code = el("code", null, cmd);
    const btn = el("button");
    btn.type = "button";
    btn._label = el("span", null, "Copy");
    btn.append(glyph("copy", "glyph g-copy"), glyph("check", "glyph g-check"), btn._label);
    btn.setAttribute("aria-label", "Copy command");
    btn.addEventListener("click", () => copyText(code.textContent, btn));
    box.append(code, btn);
    box._code = code;
    return box;
  }

  // ---------------------------------------------------------------- sparkline with crosshair

  const tip = $("tip");
  class Spark {
    constructor(label) {
      this.root = el("div", "spark");
      this.root.tabIndex = 0;
      this.root.setAttribute("role", "img");
      this.label = label;
      const svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("viewBox", "0 0 100 100");
      svg.setAttribute("preserveAspectRatio", "none");
      this.area = document.createElementNS(SVG_NS, "path");
      this.area.setAttribute("class", "area");
      this.area.setAttribute("fill", "url(#spark-fill)");
      this.line = document.createElementNS(SVG_NS, "path");
      this.line.setAttribute("class", "line");
      svg.append(this.area, this.line);
      this.hair = el("div", "hair");
      this.dot = el("div", "dot");
      this.now = el("div", "now");          // marks the latest value
      this.none = el("div", "none", "Collecting data…");
      this.hair.hidden = this.dot.hidden = this.now.hidden = true;
      this.root.append(svg, this.now, this.hair, this.dot, this.none);
      this.vals = [];
      this.index = -1;
      this.root.addEventListener("pointermove", (e) => this.hover(e.clientX));
      this.root.addEventListener("pointerdown", (e) => this.hover(e.clientX));
      this.root.addEventListener("pointerleave", () => this.clear());
      this.root.addEventListener("blur", () => this.clear());
      this.root.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        const start = this.index < 0 ? this.vals.length - 1 : this.index;
        this.show(this.nearest(start + (e.key === "ArrowLeft" ? -1 : 1), e.key === "ArrowLeft" ? -1 : 1));
      });
    }
    set(vals, t0, step, lo, hi, fmt) {
      Object.assign(this, { vals: vals || [], t0, step, lo, hi, fmt });
      this.draw();
    }
    y(v) { return 100 - 100 * Math.max(0, Math.min(1, (v - this.lo) / ((this.hi - this.lo) || 1))); }
    draw() {
      const v = this.vals, n = v.length;
      let line = "", area = "", open = false, firstX = 0, lastX = 0, gap = 99, any = false;
      for (let i = 0; i < n; i++) {
        if (v[i] == null) { gap++; continue; }
        const x = n > 1 ? (100 * i) / (n - 1) : 0, y = this.y(v[i]);
        if (open && gap > 3) { area += `L${lastX.toFixed(2)},100L${firstX.toFixed(2)},100Z`; open = false; }
        if (!open) { line += `M${x.toFixed(2)},${y.toFixed(2)}`; area += `M${x.toFixed(2)},${y.toFixed(2)}`; firstX = x; open = true; }
        else { const seg = `L${x.toFixed(2)},${y.toFixed(2)}`; line += seg; area += seg; }
        lastX = x; gap = 0; any = true;
      }
      if (open) area += `L${lastX.toFixed(2)},100L${firstX.toFixed(2)},100Z`;
      this.line.setAttribute("d", line);
      this.area.setAttribute("d", area);
      this.none.hidden = any;
      const last = this.nearest(n - 1, -1);
      this.now.hidden = last < 0;
      if (last >= 0) {
        this.now.style.left = (n > 1 ? (100 * last) / (n - 1) : 0) + "%";
        this.now.style.top = this.y(v[last]) + "%";
      }
      this.root.setAttribute("aria-label", this.label + (last >= 0 ? ": now " + this.fmt(v[last]) : ": no data yet")
        + ". Use the arrow keys to read values.");
      if (this.index >= 0) this.show(this.nearest(this.index, -1));
    }
    nearest(i, dir) {
      const v = this.vals;
      i = Math.max(0, Math.min(v.length - 1, i));
      for (let k = 0; k < v.length; k++) {
        const a = i + k * dir, b = i - k * dir;
        if (a >= 0 && a < v.length && v[a] != null) return a;
        if (b >= 0 && b < v.length && v[b] != null) return b;
      }
      return -1;
    }
    hover(clientX) {
      const r = this.root.getBoundingClientRect();
      if (!r.width || !this.vals.length) return;
      this.show(this.nearest(Math.round(((clientX - r.left) / r.width) * (this.vals.length - 1)), 1));
    }
    show(i) {
      if (i < 0) return this.clear();
      this.index = i;
      const n = this.vals.length, x = n > 1 ? (100 * i) / (n - 1) : 0, y = this.y(this.vals[i]);
      this.hair.style.left = this.dot.style.left = x + "%";
      this.dot.style.top = y + "%";
      this.hair.hidden = this.dot.hidden = false;
      const r = this.root.getBoundingClientRect();
      const strong = el("b", null, this.fmt(this.vals[i]));
      const ts = this.t0 + i * this.step;
      tip.replaceChildren(strong, document.createTextNode("  " + (this.step >= 600 ? when(ts) : clock(ts, this.step < 60))));
      tip.hidden = false;
      const px = Math.max(60, Math.min(window.innerWidth - 60, r.left + (r.width * x) / 100));
      tip.style.left = px + "px";
      tip.style.top = Math.max(28, r.top + (r.height * y) / 100 - 10) + "px";
    }
    clear() { this.index = -1; this.hair.hidden = this.dot.hidden = true; tip.hidden = true; }
  }

  // Adding #details to the address opens every agent's details and every folded section (phones fold them).
  const SHOW_ALL = location.hash === "#details";

  // ---------------------------------------------------------------- what this device may do with terminals

  let me = { local: false, watch: 404, type: 404 };
  async function loadMe() {
    try {
      const r = await fetch("/api/me", { cache: "no-store" });
      if (r.ok) { me = await r.json(); if (snap) render(); }
    } catch (e) { /* keep the previous answer */ }
  }

  // ---------------------------------------------------------------- headline tiles

  const TILES = {};
  function makeTile(id, label, withMeter) {
    const root = el("article", "tile");
    const top = el("div", "tile-top");
    const chipSlot = el("span");
    const title = el("h2", "tile-label");
    title.append(glyph(id === "mem" ? "memory" : id), document.createTextNode(label));
    top.append(title, chipSlot);
    const value = el("div", "tile-value");
    const num = el("span");
    const unit = el("small");
    value.append(num, unit);
    const spark = new Spark(label + " trend");
    const meaning = el("p", "tile-meaning");
    const meta = el("p", "tile-meta");
    root.append(top, value);
    let meter = null, fill = null;
    if (withMeter) {
      meter = el("div", "meter");
      fill = el("i");
      meter.appendChild(fill);
      root.appendChild(meter);
    }
    root.append(spark.root, meaning, meta);
    $("tiles").appendChild(root);
    TILES[id] = { root, chipSlot, num, unit, spark, meaning, meta, meter, fill, level: null };
  }
  makeTile("cpu", "Processor", true);
  makeTile("mem", "Memory", true);
  makeTile("swap", "Swap", true);
  makeTile("gpu", "Graphics card", true);
  {
    // The graphics card has two limits worth watching: how busy it is, and how full its memory is.
    const t = TILES.gpu;
    t.extra = el("div", "tile-extra");
    t.extraText = el("span");
    t.extraMeter = el("div", "meter");
    t.extraFill = el("i");
    t.extraMeter.appendChild(t.extraFill);
    t.extra.append(t.extraText, t.extraMeter);
    t.root.insertBefore(t.extra, t.meta);
    t.meaning.hidden = true;
  }

  function tileLevel(t, level) {
    if (t.level === level) return;
    t.level = level;
    const raised = level === "warn" || level === "crit";
    t.root.classList.toggle("raised", raised);
    t.chipSlot.replaceChildren(...(raised ? [chip(level)] : []));
    setClass(t.extraMeter || t.meter, "meter" + (raised ? " lv-" + level : ""));
  }
  function worst(alerts, test) {
    let lv = "ok";
    for (const a of alerts) if (test(a) && RANK[a.level] > RANK[lv]) lv = a.level;
    return lv;
  }

  function renderTiles(s) {
    const alerts = s.health.alerts;
    const c = s.cpu, m = s.mem, sw = s.swap, g = s.gpu;

    let t = TILES.cpu;
    setText(t.num, c.busy_pct == null ? "–" : Math.round(c.busy_pct) + "%");
    setText(t.unit, "busy");
    setWidth(t.fill, c.busy_pct);
    const wait = c.wait60_pct || 0;
    setText(t.meaning, wait < 5 ? "No queue: programs get the processor right away."
      : "Programs wait for the processor " + Math.round(wait) + "% of the time.");
    const busyThreads = c.busy_pct == null ? null : (c.busy_pct * c.threads) / 100;
    setText(t.meta, [busyThreads == null ? null : busyThreads.toFixed(1) + " of " + c.threads + " threads in use",
      "load " + c.load1.toFixed(1), c.temp_c == null ? null : Math.round(c.temp_c) + " °C"].filter(Boolean).join(" · "));
    tileLevel(t, worst(alerts, (a) => a.id === "cpu_wait" || a.id === "temp:cpu" || a.id === "pump"));

    t = TILES.mem;
    setText(t.num, bytes(m.used_b));
    setText(t.unit, "of " + bytes(m.total_b) + " in use");
    setWidth(t.fill, (100 * m.used_b) / (m.total_b || 1));
    setText(t.meaning, bytes(m.avail_b) + " still available."
      + (m.tmpfs_b >= 0.5 * GB ? " Temp files are holding " + bytes(m.tmpfs_b) + " of memory." : ""));
    setText(t.meta, "Also " + bytes(m.cache_b) + " of disk cache, freed automatically when needed");
    tileLevel(t, worst(alerts, (a) => ["mem_low", "mem_pressure", "ram_files", "oom_kill"].includes(a.id)));

    t = TILES.swap;
    const swapPct = sw.total_b ? (100 * sw.used_b) / sw.total_b : 0;
    setText(t.num, sw.total_b ? bytes(sw.used_b) : "–");
    setText(t.unit, sw.total_b ? "of " + bytes(sw.total_b) + " in use" : "no swap");
    setWidth(t.fill, swapPct);
    setText(t.meaning, !sw.total_b ? "This PC has no swap space."
      : sw.used_b < 64 * MB ? "Not needed right now: everything fits in memory."
        : Math.round(swapPct) + "% full." + (sw.in_bps > MB ? " Reading back " + rate(sw.in_bps) + ", which slows things down." : ""));
    setText(t.meta, sw.out_bps > MB ? "Moving " + rate(sw.out_bps) + " out of memory"
      : "Swap is overflow memory on disk, used when memory runs short");
    tileLevel(t, worst(alerts, (a) => a.id === "swap_high" || a.id === "swap_in"));

    t = TILES.gpu;
    if (g.ok) {
      setText(t.num, g.util_pct == null ? "–" : Math.round(g.util_pct) + "%");
      setText(t.unit, "busy");
      setWidth(t.fill, g.util_pct);
      setWidth(t.extraFill, (100 * g.vram_used_b) / (g.vram_total_b || 1));
      setText(t.extraText, "Graphics memory: " + bytes(g.vram_used_b) + " of " + bytes(g.vram_total_b) + " in use");
      const big = (g.procs || []).find((p) => p.vram_b >= 300 * MB);
      setText(t.meta, [g.temp_c == null ? null : Math.round(g.temp_c) + " °C", g.power_w == null ? null : Math.round(g.power_w) + " W",
        big ? "most: " + big.name + " " + bytes(big.vram_b) : null].filter(Boolean).join(" · "));
    } else {
      setText(t.num, "–");
      setText(t.unit, "");
      setWidth(t.fill, 0);
      setWidth(t.extraFill, 0);
      setText(t.extraText, "No reading from the graphics card.");
      setText(t.meta, "");
    }
    tileLevel(t, worst(alerts, (a) => a.section === "gpu" || a.id === "temp:gpu"));
  }

  function renderSparks(h, s) {
    if (!h) return;
    const se = h.series || {};
    const pct = (v) => Math.round(v) + "%";
    const mb = (v) => bytes(v * MB);
    TILES.cpu.spark.set(se.cpu_busy_pct, h.t0, h.step_s, 0, 100, pct);
    TILES.mem.spark.set(se.mem_used_mb, h.t0, h.step_s, 0, s.mem.total_b / MB, mb);
    TILES.swap.spark.set(se.swap_used_mb, h.t0, h.step_s, 0, Math.max(1, s.swap.total_b / MB), mb);
    TILES.gpu.spark.set(se.gpu_util_pct, h.t0, h.step_s, 0, 100, pct);
  }

  // ---------------------------------------------------------------- banner

  // Where an alert points: [what to call the place, id of the card that shows it].
  const SECTION = {
    disks: ["Disks", "disks-card"], memory: ["Memory", "tiles"], cpu: ["Processor", "tiles"], gpu: ["Graphics card", "tiles"],
    temps: ["Temperatures", "temps-card"], agents: ["Agents", "agents-card"], leftovers: ["Left behind", "leftovers-card"],
  };
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
  /** No gliding: the person asked the system for less motion, or the Simple look is on. */
  const still = () => REDUCED || !glassLook();
  // The colour phones give the browser's own bar, [light, dark]: the backdrop's tint in Glass, the header's in Simple.
  const THEME_COLOR = { ok: ["#e9eef7", "#0c1322"], warn: ["#f6efdf", "#161109"], crit: ["#f6e9e9", "#1a0c0d"] };
  const THEME_FLAT = ["#fcfcfb", "#1b1c20"];
  function paintBrowserBar() {
    const bar = glassLook() ? THEME_COLOR[root.dataset.level] || THEME_COLOR.ok : THEME_FLAT;
    $("tc-light").setAttribute("content", bar[0]);
    $("tc-dark").setAttribute("content", bar[1]);
  }

  /** "What is wrong. Why, or what to do about it." -> [the first sentence, the rest]. */
  function splitMessage(msg) {
    const m = /^(.+?[.!?])\s+(?=[A-Z])/.exec(msg);
    return m ? [m[1], msg.slice(m[0].length)] : [msg, ""];
  }
  /** Scroll a card into view and outline it once, so the eye lands on it. */
  function reveal(id) {
    const target = id && $(id);
    if (!target) return;
    if (target.tagName === "DETAILS") target.open = true;
    target.scrollIntoView({ behavior: still() ? "auto" : "smooth", block: "start" });
    target.classList.remove("flash");
    void target.offsetWidth;                  // so the outline plays again on a second press
    target.classList.add("flash");
  }
  document.addEventListener("animationend", (e) => {      // one-shot effects clean up after themselves
    if (e.target instanceof Element) e.target.classList.remove("flash", "pulse", "enter");
  });

  function renderBanner(s) {
    const hl = s.health;
    const banner = $("banner"), orb = $("banner-icon");
    if (banner.dataset.level !== hl.level) {
      if (banner.dataset.level) {             // the level changed while the page was open: the orb glows a few times
        orb.classList.remove("pulse");
        void orb.offsetWidth;
        orb.classList.add("pulse");
      }
      banner.dataset.level = hl.level;
    }
    if (root.dataset.level !== hl.level) {
      root.dataset.level = hl.level;          // tints the backdrop (Glass look)
      paintBrowserBar();
    }
    setIcon(orb, hl.level);
    setIcon($("brand-orb"), hl.level);          // the same level, still in view once the banner has scrolled away
    setText($("brand-word"), LEVEL_WORD[hl.level]);
    setText($("headline"), hl.headline);
    const nCrit = hl.alerts.filter((a) => a.level === "crit").length, nWarn = hl.alerts.length - nCrit;
    setText($("subline"), !hl.alerts.length ? "Nothing needs your attention."
      : [nCrit ? nCrit + " critical" : "", nWarn ? nWarn + (nWarn === 1 ? " warning" : " warnings") : ""].filter(Boolean).join(" · "));

    // The agent summary sits apart from the problems, as a row of small counts.
    const n = s.agent_counts;
    const counts = !n.total ? [["none", "", "No agents running"]]
      : [["total", n.total, n.total === 1 ? "agent" : "agents"], ["waiting", n.waiting, "waiting for you"],
        ["working", n.working, "working"], ["idle", n.total - n.working - n.waiting, "idle"]].filter((c) => c[1] > 0);
    sync($("counts"), counts, (c) => c[0], (c) => {
      const stated = c[0] !== "total" && c[0] !== "none";
      const li = el("li", "count" + (stated ? " st-" + c[0] : ""));
      if (stated) li.appendChild(el("span", "mark"));
      li._n = el("b");
      li._t = el("span");
      li.append(li._n, li._t);
      return li;
    }, (li, c) => { setText(li._n, c[1]); setText(li._t, c[2]); });

    // One row per problem: what kind, where and for how long; then what is wrong; then why and the fix.
    sync($("alerts"), hl.alerts, (a) => a.id, () => {
      const li = el("li", "alert");
      const meta = el("div", "alert-meta");
      li._slot = el("span", "icon-slot");
      li._level = el("span", "alert-level");
      li._where = el("span", "alert-where");
      li._since = el("span", "alert-since");
      li._show = el("button", "alert-show");
      li._show.type = "button";
      li._show.append(document.createTextNode("Show"), glyph("down"));
      li._show.addEventListener("click", () => reveal(li._target));
      meta.append(li._slot, li._level, li._where, li._since, li._show);
      li._title = el("p", "alert-title");
      li._detail = el("p", "alert-detail");
      li.append(meta, li._title, li._detail);
      return li;
    }, (li, a) => {
      li.classList.toggle("lv-warn", a.level === "warn");
      li.classList.toggle("lv-crit", a.level === "crit");
      setIcon(li._slot, a.level);
      setText(li._level, LEVEL_WORD[a.level]);
      const place = SECTION[a.section];
      setText(li._where, place ? place[0] : "");
      li._where.hidden = !place;
      li._target = place ? place[1] : null;
      li._show.hidden = !place;
      if (place) li._show.setAttribute("aria-label", "Show " + place[0]);
      const age = s.ts - a.since;
      setText(li._since, !a.since ? "" : age < 90 ? "just now" : "for " + longDur(age));
      li._since.hidden = !a.since;
      const [title, rest] = splitMessage(a.msg);
      setText(li._title, title);
      setText(li._detail, rest);
      li._detail.hidden = !rest;
      if (a.cmd && !li._fix) {
        li._fix = el("div", "alert-fix");
        li._cmd = commandBox(a.cmd);
        li._fix.append(el("span", "alert-fix-label", "To fix it, run this in a terminal:"), li._cmd);
        li.appendChild(li._fix);
      }
      if (li._fix) { if (a.cmd) setText(li._cmd._code, a.cmd); li._fix.hidden = !a.cmd; }
    });
    const mark = hl.level === "ok" ? "✓" : hl.level === "warn" ? "▲" : "✕";
    const title = mark + " " + hl.headline + " · PC Health";
    if (document.title !== title) document.title = title;
    const fav = "/favicon" + (hl.level === "ok" ? "" : "-" + hl.level) + ".svg";
    if ($("favicon").getAttribute("href") !== fav) $("favicon").setAttribute("href", fav);
  }

  // ---------------------------------------------------------------- disks

  function renderDisks(s) {
    const alerts = s.health.alerts;
    const disks = s.disks.filter((d) => !d.is_ram);
    setText($("disks-note"), "Files in /tmp are held in memory, so they are counted under Memory");
    sync($("disks"), disks, (d) => d.mount, () => {
      const row = el("div", "disk");
      const top = el("div", "disk-top");
      row._name = el("span", "disk-name");
      row._free = el("span", "disk-free");
      top.append(row._name, row._free);
      row._meter = el("div", "meter");
      row._fill = el("i");
      row._meter.appendChild(row._fill);
      row._sub = el("div", "disk-sub");
      row.append(top, row._meter, row._sub);
      return row;
    }, (row, d) => {
      row._name.replaceChildren(document.createTextNode(d.label),
        ...(d.label !== d.mount ? [el("span", "muted", d.mount)] : []));
      setText(row._free, bytes(d.avail_b) + " free of " + bytes(d.total_b));
      const lv = worst(alerts, (a) => a.id.endsWith(":" + d.mount) && a.section === "disks");
      setClass(row._meter, "meter" + (lv !== "ok" ? " lv-" + lv : ""));
      setWidth(row._fill, d.used_pct);
      row._meter.title = Math.round(d.used_pct) + "% full";
      const sub = [];
      if (d.eta_s) sub.push(chip(d.eta_s < 8 * 3600 ? "crit" : d.eta_s < 48 * 3600 ? "warn" : "info", "Full in about " + longDur(d.eta_s) + " at this pace"));
      if (d.change_24h_b != null && Math.abs(d.change_24h_b) >= 0.2 * GB) sub.push(el("span", null, signedBytes(d.change_24h_b) + " free space in 24 h"));
      if (d.change_1h_b != null && Math.abs(d.change_1h_b) >= 0.5 * GB) sub.push(el("span", null, signedBytes(d.change_1h_b) + " in the last hour"));
      else if (d.change_1h_b == null && d.change_10m_b != null && Math.abs(d.change_10m_b) >= 0.3 * GB) sub.push(el("span", null, signedBytes(d.change_10m_b) + " in the last 10 minutes"));
      if (d.write_bps > MB) sub.push(el("span", null, "writing " + rate(d.write_bps)));
      if (!sub.length) sub.push(el("span", "muted", d.change_24h_b != null ? "Steady"
        : d.change_1h_b != null ? "Steady over the last hour" : "Collecting a trend…"));
      const sig = sub.map((n) => n.textContent).join("|");
      if (row._sig !== sig) { row._sig = sig; row._sub.replaceChildren(...sub); }
      row.classList.toggle("has-news", !!d.eta_s || lv !== "ok");
    });
  }

  // ---------------------------------------------------------------- agents

  // [key, label for the phone layout, right-aligned number?, spans the full width on phones?]
  const AGENT_COLS = [["agent", "Agent", false, true], ["state", "State", false, false], ["tmux", "tmux", false, false],
    ["cpu", "Processor", true, false], ["mem", "Memory", true, false], ["vram", "Graphics memory", true, false],
    ["tmp", "Temp files in memory", true, false], ["write", "Writing to disk", true, false],
    ["ctx", "Context", true, false], ["doing", "Running now", false, true], ["up", "Up for", true, false]];

  // How Claude Code names the kinds of sub-agent, in words for the page.
  const SUB_KIND = { "general-purpose": "General", fork: "Fork", "workflow-subagent": "Workflow" };

  function tokenCount(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (n >= 1e8) return Math.round(n / 1e6) + "M";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e4) return Math.round(n / 1e3) + "k";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  function renderAgents(s) {
    const threads = s.cpu.threads;
    const n = s.agent_counts;
    setText($("agents-note"), n.total ? n.total + (n.total === 1 ? " agent" : " agents") + ": waiting for you first, then working, then idle (most recent first)" : "");
    $("agents-empty").hidden = s.agents.length > 0;
    $("agents").hidden = s.agents.length === 0;
    const blank = (td, text) => { setText(td, text); td.classList.toggle("blank", text === "–" || text === ""); };
    // An agent's sub-agents, the ones running right now, are listed directly under it as lighter rows of their own.
    const rows = [];
    for (const a of s.agents) {
      rows.push(a);
      for (const x of a.subagents || []) rows.push({ sub: x, of: a, id: a.id + "/sub/" + x.id });
    }
    sync($("agents-body"), rows, (a) => a.id, () => {
      const tr = el("tr");
      const c = tr._c = {};
      for (const [key, label, num, wide] of AGENT_COLS) {
        const main = key === "agent" || key === "state" || key === "tmux";      // what a phone shows by default
        const td = el("td", "c-" + key + (main ? "" : " detail") + (num ? " num" : "") + (wide ? " wide" : ""));
        if (key !== "agent") td.dataset.label = label;
        tr.appendChild(td);
        c[key] = td;
      }
      // Phones list agents in two short lines; this button (and a tap on the row) shows the rest.
      tr._more = el("button", "more");
      tr._more.type = "button";
      tr._more.setAttribute("aria-expanded", "false");
      tr._more.setAttribute("aria-label", "Show details");
      tr._more.appendChild(glyph("chevron"));
      tr.addEventListener("click", (e) => {
        if (e.target.closest("a, button:not(.more)")) return;
        if (!matchMedia("(max-width: 640px)").matches) return;
        const open = tr.classList.toggle("open");
        tr._more.setAttribute("aria-expanded", String(open));
        tr._more.setAttribute("aria-label", open ? "Hide details" : "Show details");
      });
      const top = el("div", "agent-top");
      tr._name = el("div", "agent-name");
      tr._saved = savedMark();
      top.append(tr._name, tr._saved);
      tr._sub = el("div", "agent-sub");
      c.agent.append(top, tr._sub);
      tr._state = el("span", "state");
      tr._word = el("span");
      tr._state.append(el("span", "mark"), tr._word);
      tr._note = el("div", "state-note");
      c.state.append(tr._state, tr._note, tr._more);
      tr._cpu = el("span");
      tr._mini = el("i");
      const mini = el("div", "mini");
      mini.appendChild(tr._mini);
      c.cpu.append(tr._cpu, mini);
      tr._mem = el("div");
      tr._swap = el("div", "muted");
      c.mem.append(tr._mem, tr._swap);
      c.doing.classList.add("doing");
      return tr;
    }, (tr, a) => {
      const c = tr._c;
      if (SHOW_ALL && !tr._shown) { tr._shown = true; tr.classList.add("open"); tr._more.setAttribute("aria-expanded", "true"); }
      if (a.sub) {
        // A sub-agent runs inside its agent's own process: there is no processor or memory figure of its own.
        // What is known: what it was asked, its kind, the tool it is waiting on, its own context, how long it has run.
        const x = a.sub;
        tr.classList.add("sub", "nested");
        setText(tr._name, x.what || "Sub-agent");
        tr._saved.hidden = true;
        const sig = "sub|" + x.kind + "|" + a.of.label;
        if (tr._sig !== sig) {
          tr._sig = sig;
          tr._sub.replaceChildren(el("span", "tag", "Sub-agent"), ...(x.kind ? [el("span", "tag", SUB_KIND[x.kind] || x.kind)] : []));
          tr.title = "A sub-agent that " + a.of.label + " is running";
          c.tmux.replaceChildren();
        }
        setClass(tr._state, "state st-working");
        setText(tr._word, "Working");
        const quiet = s.ts - x.active;
        setText(tr._note, quiet >= 90 ? "quiet for " + dur(quiet) : "");
        setText(tr._cpu, "");
        c.cpu.classList.add("blank");
        setText(tr._mem, "");
        setText(tr._swap, "");
        c.mem.classList.add("blank");
        blank(c.vram, "");
        blank(c.tmp, "");
        blank(c.write, "");
        blank(c.ctx, x.ctx ? tokenCount(x.ctx) : "–");
        c.ctx.title = x.ctx ? "Tokens in this sub-agent's own conversation right now" : "";
        blank(c.doing, x.doing || "thinking");
        c.doing.title = x.doing ? "The tool it is waiting on" : "No tool is running for it: it is reading a result or writing";
        blank(c.up, x.started ? dur(s.ts - x.started) : "–");
        return;
      }
      tr.classList.toggle("nested", !!a.parent);
      tr.classList.toggle("attention", a.state === "waiting");
      setText(tr._name, a.label);
      setSavedMark(tr._saved, a.saved ? "yes" : null, s);
      const term = a.term || null;
      const canOpen = !!term && s.terminals.live && me.type !== 404 && me.type !== 403;
      const canWatch = !!term && me.watch !== 404 && me.watch !== 403;
      const sig = [a.title, a.host, a.kind, a.model].join("|");
      if (tr._sig !== sig) {
        tr._sig = sig;
        const bits = [];
        if (a.title) bits.push(el("span", null, a.title));
        bits.push(el("span", "tag", a.kind === "codex" ? "Codex" : "Claude Code"));
        bits.push(el("span", "tag", a.host === "emdash" ? "Emdash" : "Terminal"));
        if (a.model) bits.push(el("span", "tag", a.model));
        tr._sub.replaceChildren(...bits);
      }
      // tmux column: is this agent inside tmux, and if so the two ways to reach it.
      const tsig = [term ? term.session : "", canOpen, canWatch].join("|");
      if (tr._tsig !== tsig) {
        tr._tsig = tsig;
        const bits = [];
        if (!term) {
          const no = el("span", "muted", "No");
          no.title = "Not started inside tmux, so it cannot be opened from here";
          bits.push(no);
        } else {
          const ways = el("span", "ways");                    // the same two buttons as in the Terminals list
          if (canWatch) ways.appendChild(wayIn("view", term.session, !canOpen));
          if (canOpen) ways.appendChild(wayIn("type", term.session, true));
          bits.push(ways.firstChild ? ways : el("span", null, "Yes"));
        }
        tr._c.tmux.replaceChildren(...bits);
      }
      setClass(tr._state, "state st-" + a.state);
      const word = STATE_WORD[a.state] || "Running";
      setText(tr._word, word);
      let note = a.state_text.startsWith(word) ? a.state_text.slice(word.length).replace(/^[:,]?\s*/, "") : a.state_text;
      if (a.state_since && a.state !== "working") note = (note ? note + " · " : "") + "for " + dur(s.ts - a.state_since);
      setText(tr._note, note);
      setText(tr._cpu, a.cpu_cores >= 0.05 ? cores(a.cpu_cores) : "–");
      c.cpu.classList.toggle("blank", a.cpu_cores < 0.05);
      setWidth(tr._mini, (100 * a.cpu_cores) / threads);
      setText(tr._mem, bytes(a.mem_b));
      setText(tr._swap, a.swap_b > 50 * MB ? "+ " + bytes(a.swap_b) + " in swap" : "");
      blank(c.vram, a.vram_b > MB ? bytes(a.vram_b) : "–");
      blank(c.tmp, a.tmp_b > MB ? bytes(a.tmp_b) : "–");
      blank(c.write, rate(a.write_bps));
      // Only what the conversation holds right now; the running total is not shown.
      const t = a.tokens;
      blank(c.ctx, t && t.ctx ? tokenCount(t.ctx) : !t && a.kind === "claude" && a.state !== "unknown" ? "counting…" : "–");
      c.ctx.title = t && t.ctx ? "Tokens in this session's conversation right now, read from its transcript" : "";
      const doing = a.doing.map((d) => d.name + (d.n > 1 ? " ×" + d.n : "")).join(", ");
      const helpers = a.helpers ? a.helpers + " detached helper" + (a.helpers > 1 ? "s" : "") : "";
      const subs = (a.subagents || []).length;
      blank(c.doing, [doing, helpers, subs ? subs + (subs > 1 ? " sub-agents" : " sub-agent") : ""].filter(Boolean).join(" · ") || "–");
      setText(c.up, dur(s.ts - a.started));
    });
    const ended = s.ended || [];
    $("ended-box").hidden = ended.length === 0;
    setText($("ended-summary"), "Ended in the last 24 hours (" + ended.length + ")");
    sync($("ended"), ended, (e) => e.id + ":" + e.ended, () => el("li"), (li, e) => {
      setText(li, e.label + (e.title ? " (" + e.title + ")" : "") + " ended " + when(e.ended) + " after " + longDur(e.ran_s));
    });
  }

  // ---------------------------------------------------------------- the two ways into a tmux session

  function terminalUrl(session) { return "/terminal?s=" + encodeURIComponent(session); }       // open it and type
  function watchUrl(session) { return "/watch?s=" + encodeURIComponent(session); }             // view only
  /** One of the two ways in, as a button that is only a symbol: an eye to view the session without typing,
   *  a keyboard to open it and type. The same button, in that order, wherever a session can be reached from.
   *  `tone` tints the main one. It says what it is on hover. */
  function wayIn(kind, session, tone) {
    const view = kind === "view";
    const a = el("a", "btn small sym" + (tone ? " tone" : ""));          // not "icon": that class is the status icons'
    a.href = view ? watchUrl(session) : terminalUrl(session);
    a.title = view ? "View " + session + ": look, without typing" : "Open " + session + ": type in it";
    a.setAttribute("aria-label", a.title);
    a.appendChild(glyph(view ? "eye" : "keyboard"));
    return a;
  }
  {
    const old = /^#watch=([A-Za-z0-9_.-]{1,64})$/.exec(location.hash);       // an address saved from an earlier version
    if (old) location.replace(watchUrl(old[1]));
  }

  // ---------------------------------------------------------------- terminals (tmux sessions)

  /** A line icon from the sprite at the top of index.html, drawn in the text colour. */
  function glyph(name, cls) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", cls || "glyph");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#g-" + name);
    svg.appendChild(use);
    return svg;
  }

  // ---------------------------------------------------------------- starting, renaming and closing terminals
  // PC Health's own server only reads. These three things are done by the terminal service (ttyd running
  // terminal/attach.sh), the same part that lets you type. This page asks it over the connection PC Health
  // already carries under /tty, so whoever may type may ask, and nobody else.

  /** One request to the terminal service. -> { ok, text }: the name it settled on, or why it refused. */
  function terminalService(args, waitMs) {
    return new Promise((resolve) => {
      let said = "", settled = false, ws = null;
      const finish = (ok, text) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { if (ws) ws.close(); } catch (e) { /* already closed */ }
        resolve({ ok, text });
      };
      const timer = setTimeout(() => finish(false, "The terminal service did not answer."), waitMs || 8000);
      try {
        const query = args.map((a) => "arg=" + encodeURIComponent(a)).join("&");
        ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/tty/ws?" + query, ["tty"]);
      } catch (e) { return finish(false, "This browser could not reach the terminal service."); }
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send(new TextEncoder().encode(JSON.stringify({ AuthToken: "", columns: 120, rows: 24 })));
      ws.onmessage = (ev) => {
        const data = new Uint8Array(ev.data);
        if (data[0] !== 48) return;                 // "0" marks what the script printed; titles and settings are not for us
        said += new TextDecoder().decode(data.subarray(1));
        const line = /PCH-(OK|ERROR) ([^\r\n]*)\r?\n/.exec(said);
        if (line) finish(line[1] === "OK", line[2]);
      };
      ws.onclose = () => finish(false, "The terminal service is not answering. Is it running?");
    });
  }

  const sheet = $("sheet");
  const NEW_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;        // no dots: tmux turns them into "_", so the name would not be found again
  const GO_WORD = { new: "Create and open", rename: "Rename", close: "Close terminal" };
  let sheetJob = null;                // { kind: "new" | "rename" | "close" | "info", name, busy, facts, ... }

  /** What this device may do with terminals right now. */
  function terminalRights(s) {
    const t = (s && s.terminals) || {};
    const canType = !!t.live && me.type !== 404 && me.type !== 403;
    return { canType, canWatch: me.watch !== 404 && me.watch !== 403, canManage: canType && me.type === 200 };
  }
  /** The facts about a session that do not need to be on screen all the time. */
  function sessionInfo(x) {
    if (!x.w) return "";
    return x.w + "×" + x.h + (x.windows > 1 ? " · " + x.windows + " windows" : "")
      + (x.attached ? " · open in " + x.attached + (x.attached === 1 ? " place" : " places") : " · not open anywhere");
  }
  /** A status as a pill: the mark, the word, and for how long. The same pill in the Agents table, in each
   *  terminal's row and in its summary. */
  function statePill(state, word, since) {
    const p = el("span", "state st-" + state);
    p._word = el("span", null, word);
    p._since = el("span", "state-for", since || "");
    p.append(el("span", "mark"), p._word, p._since);
    return p;
  }
  /** The summary of a terminal, from what the page already knows:
   *  the status, the conversation's title, a few numbers (stats) and a few longer facts (rows). */
  function sessionFacts(x, a, ts, kept) {
    const f = { state: a ? a.state : "none", word: a ? STATE_WORD[a.state] || "Running" : "No agent", since: "", detail: "",
      title: "", stats: [], rows: [] };
    const stat = (label, value) => { if (value) f.stats.push([label, value]); };
    if (a) {
      const said = a.state_text || "";
      f.detail = said.startsWith(f.word) ? said.slice(f.word.length).replace(/^[:,]?\s*/, "") : said;
      f.since = a.state !== "working" && a.state_since ? "for " + longDur(ts - a.state_since) : "";
      f.title = a.title || "";
      stat("Agent", a.kind === "codex" ? "Codex" : "Claude Code");
      stat("Model", a.model);
      stat("Context", a.tokens && a.tokens.ctx ? tokenCount(a.tokens.ctx) + " tokens" : "");
      stat("Running for", longDur(ts - a.started));
      stat("Processor", a.cpu_cores >= 0.05 ? cores(a.cpu_cores) : "");
      stat("Memory", a.mem_b ? bytes(a.mem_b) + (a.swap_b > 50 * MB ? " + " + bytes(a.swap_b) + " in swap" : "") : "");
      stat("Graphics memory", a.vram_b > MB ? bytes(a.vram_b) : "");
      stat("Temp files", a.tmp_b > MB ? bytes(a.tmp_b) : "");
      if (a.folder) f.rows.push(["Folder", a.folder, "path", a.branch]);
      const doing = [a.doing.map((d) => d.name + (d.n > 1 ? " ×" + d.n : "")).join(", "),
        a.helpers ? a.helpers + " detached helper" + (a.helpers > 1 ? "s" : "") : ""].filter(Boolean).join(" · ");
      if (doing) f.rows.push(["Doing now", doing]);
      const subs = (a.subagents || []).map((x) => (x.what || "Sub-agent") + (x.kind ? " (" + (SUB_KIND[x.kind] || x.kind) + ")" : ""));
      if (subs.length) f.rows.push(["Sub-agents", subs.join(" · ")]);
    } else f.detail = "running " + (x.cmd || "a shell");
    const where = [x.emdash ? "started by Emdash" : "", sessionInfo(x)].filter(Boolean).join(" · ");
    if (where) f.rows.push(["Terminal", where]);
    if (x.saved && kept) f.rows.push(["Saved", when(kept.ts) + (x.saved === "yes" ? (a ? ", with this conversation" : "")
      : x.saved === "other" ? ", with something else than what is here now. " + savedOtherHint(snap)
        : x.saved === "shell" ? ", as a terminal only: the agent running in it is not saved."
          : ". The conversation saved with it is not running here now.")]);
    return f;
  }
  /** The summary as page content: the status line, a grid of small numbers, then the longer facts. */
  function infoParts(f) {
    const head = el("div", "info-head");
    head.appendChild(statePill(f.state, f.word, f.since));
    if (f.detail) head.appendChild(el("span", "info-detail", f.detail));
    const parts = [head];
    if (f.stats.length) {
      const dl = el("dl", "info-stats");
      for (const [label, value] of f.stats) {
        const cell = el("div", "info-stat");
        cell.append(el("dt", null, label), el("dd", null, value));
        dl.appendChild(cell);
      }
      parts.push(dl);
    }
    if (f.rows.length) {
      const dl = el("dl", "info-rows");
      for (const [label, value, cls, tag] of f.rows) {
        const dd = el("dd", cls || null, value);
        if (tag) dd.appendChild(el("span", "tag", tag));
        dl.append(el("dt", null, label), dd);
      }
      parts.push(dl);
    }
    return parts;
  }
  /** Fill the summary with the last thing asked and the last answer, read from the conversation's record. */
  async function sessionLastWords(job) {
    const cut = (t) => { t = t.replace(/\s+/g, " ").trim(); return t.length > 320 ? t.slice(0, 320).trimEnd() + " …" : t; };
    try {
      const r = await fetch("/api/conversation?session=" + encodeURIComponent(job.name), { cache: "no-store" });
      if (!r.ok || sheetJob !== job) return;
      const items = (await r.json()).items || [];
      if (sheetJob !== job) return;
      const last = (role) => { for (let i = items.length - 1; i >= 0; i--) if (items[i].r === role) return items[i]; return null; };
      const asked = last("you"), answered = last("agent");
      if (!asked && !answered) return;
      const said = (who, text, cls) => {
        const box = el("div", "info-said " + cls);
        box.append(el("div", "info-who", who), el("div", "info-text", text));
        return box;
      };
      const talk = el("div", "info-talk");
      talk.appendChild(el("h3", null, "Last exchange"));
      if (asked) talk.appendChild(said("You asked", cut(asked.t), "you"));
      if (answered) talk.appendChild(said("It answered", cut(answered.t), "agent"));
      $("sheet-info").appendChild(talk);
    } catch (e) { /* the summary stands without it */ }
  }
  /** What is known right now about a session someone is about to rename or close. */
  function sessionJob(kind, name) {
    const x = ((snap && snap.terminals.sessions) || []).find((it) => it.name === name) || {};
    const a = x.agent ? snap.agents.find((it) => it.id === x.agent) : null;
    const busy = !a ? "" : a.state === "working" ? "An agent is working in it right now."
      : a.state === "waiting" ? "An agent in it is waiting for you." : "An agent is open in it.";
    const may = terminalRights(snap);
    return { kind, name, busy, facts: sessionFacts(x, a, snap ? snap.ts : 0, snap && snap.terminals.saved),
      record: !!a && a.kind === "claude" && may.canWatch,
      canType: may.canType && !!x.openable, canWatch: may.canWatch && !!x.openable, canManage: may.canManage && !x.emdash && !!x.openable };
  }
  function sheetError(text) {
    $("sheet-error").hidden = !text;
    setText($("sheet-error"), text);
  }
  function sheetShow(job) {
    sheetJob = job;
    const typing = job.kind === "new" || job.kind === "rename";
    const input = $("sheet-name"), go = $("sheet-go");
    sheetError("");
    $("sheet-choices").hidden = job.kind !== "info";
    $("sheet-info").hidden = job.kind !== "info";
    $("sheet-info").replaceChildren();
    if (job.kind === "info") {                  // the summary, then everything that can be done with it, in words
      $("sheet-info").append(...infoParts(job.facts));
      if (job.record) sessionLastWords(job);
      $("sheet-pick-open").hidden = !job.canType;
      $("sheet-pick-view").hidden = !job.canWatch;
      $("sheet-pick-open").href = terminalUrl(job.name);
      $("sheet-pick-view").href = watchUrl(job.name);
      $("sheet-pick-rename").hidden = $("sheet-pick-close").hidden = !job.canManage;
    }
    $("sheet-field").hidden = !typing;
    go.hidden = job.kind === "info";
    go.disabled = false;
    setClass(go, "btn " + (job.kind === "close" ? "danger" : "primary"));
    setText(go, GO_WORD[job.kind] || "");
    setText($("sheet-title"), job.kind === "new" ? "New terminal" : job.kind === "rename" ? "Rename " + job.name
      : job.kind === "close" ? "Close " + job.name + "?" : job.name);
    setText($("sheet-cancel"), job.kind === "info" ? "Done" : "Cancel");
    setText($("sheet-text"), job.kind === "new" ? "A new tmux session that starts in your home folder (~). Give it a name: that is how you will find it here and in tmux."
      : job.kind === "close" ? "This ends the tmux session and everything running in it. It cannot be undone." + (job.busy ? " " + job.busy : "")
        : job.kind === "info" ? job.facts.title : "");          // under a terminal's name: the title of its conversation
    setText($("sheet-label"), job.kind === "rename" ? "New name" : "Name");
    input.value = job.kind === "rename" ? job.name : "";
    if (!sheet.open) sheet.showModal();
    if (typing) { input.focus(); input.select(); } else if (job.kind === "close") $("sheet-cancel").focus();
  }
  $("sheet-cancel").addEventListener("click", () => sheet.close());
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.close(); });            // a click outside
  $("sheet-pick-rename").addEventListener("click", () => sheetShow(Object.assign({}, sheetJob, { kind: "rename" })));
  $("sheet-pick-close").addEventListener("click", () => sheetShow(Object.assign({}, sheetJob, { kind: "close" })));
  $("term-new").addEventListener("click", () => sheetShow({ kind: "new" }));
  $("sheet-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const job = sheetJob, go = $("sheet-go");
    if (!job || job.kind === "info" || go.disabled) return;
    const name = $("sheet-name").value.trim();
    if (job.kind !== "close") {
      if (!name) return sheetError("Give it a name first.");
      if (!NEW_NAME.test(name)) return sheetError("Use letters, digits, dashes and underscores, without spaces or dots, starting with a letter or digit.");
      if (job.kind === "rename" && name === job.name) return sheet.close();
    }
    sheetError("");
    go.disabled = true;
    const done = await terminalService(job.kind === "new" ? ["--new", name] : job.kind === "rename" ? ["--rename", job.name, name] : ["--close", job.name]);
    if (!done.ok) {
      go.disabled = false;
      return sheetError(done.text);
    }
    try { await fetch("/api/snapshot?rescan=1", { cache: "no-store" }); } catch (err) { /* the next refresh shows it */ }
    if (job.kind === "new") { location.href = terminalUrl(done.text) + "&new=1"; return; }       // straight into it
    sheet.close();
    setTimeout(poll, 500);
    setTimeout(poll, 1600);
  });

  // ---------------------------------------------------------------- saved terminals
  // Settings can note the terminals that are open, with the conversation in each, and bring them back later
  // (after a restart of the PC, when tmux has lost them all). Both are done by the terminal service, like
  // starting or closing a terminal: this page only asks, and shows what the monitor reports as saved.

  /** What to do about a terminal saved with something else than what is in it now: nothing, when saving is automatic. */
  function savedOtherHint(s) {
    return s && s.terminals && s.terminals.auto ? "What is here now is saved by itself within a minute." : "Save again to keep this.";
  }
  /** A small bookmark: "this one is saved". Filled when what is saved is what is there now. */
  function savedMark() {
    const m = el("span", "saved-mark");
    m.hidden = true;
    m.appendChild(glyph("bookmark"));
    return m;
  }
  function setSavedMark(m, verdict, s) {
    const kept = s.terminals && s.terminals.saved;
    m.hidden = !verdict || !kept;
    if (m.hidden) return;
    setClass(m, "saved-mark " + verdict);
    const title = "Saved " + whenIn(kept.ts) + (verdict === "yes" ? ". Restore saved terminals (in Settings) brings it back after a restart."
      : verdict === "other" ? ", but with something else than what is here now. " + savedOtherHint(s)
        : verdict === "shell" ? ", as a terminal only. Restore brings the terminal back, not the agent running in it."
          : ". The conversation saved with it is not running here now.");
    if (m.title !== title) { m.title = title; m.setAttribute("aria-label", title); }
  }

  // "save" | "restore" while one runs; the last answer as [it worked, what was said, asked from "settings" | "card"]
  let keepBusy = "", keepAsk = false, keepSaid = null, keepTimer = 0, loginBusy = false;
  /** Terminals that are open, but without the conversation that was saved with them (Claude closed, or not up yet). */
  function savedButStopped(s) {
    return ((s && s.terminals && s.terminals.sessions) || []).filter((x) => x.saved === "stopped").map((x) => x.name);
  }
  function renderKeep(s, canManage) {
    const kept = (s.terminals && s.terminals.saved) || null;
    const running = ((s.terminals && s.terminals.sessions) || []).length;
    const gone = kept ? kept.gone : [];
    const stopped = kept ? savedButStopped(s) : [];
    $("keep").hidden = !canManage;
    const few = (names) => names.slice(0, 6).join(", ") + (names.length > 6 ? " and " + (names.length - 6) + " more" : "");
    // Automatic: the monitor keeps the list in step with the open terminals, and never drops one merely because
    // it is gone. The button is then what takes the ones that are not coming back off the list.
    const auto = !!(s.terminals && s.terminals.auto), loose = gone.length + stopped.length;
    setText($("keep-note"), !kept ? (auto ? "The terminals that are open are saved by themselves, with the conversation in each. After a restart of the PC, one press brings them all back."
      : "Save the terminals that are open now, with the conversation in each. After a restart of the PC, one press brings them all back.")
      : (auto ? "Saved automatically. Last change " : "Saved ") + whenIn(kept.ts) + ": " + few(kept.names) + "."
        + (gone.length ? " Not running now: " + few(gone) + "." : "")
        + (stopped.length ? " Open, but without the conversation saved with it: " + few(stopped) + "." : "")
        + (!loose ? " All of them are running." : ""));
    setText($("keep-save-word"), auto ? (keepAsk ? "Forget them" : "Forget what is not running")
      : keepAsk ? "Save anyway" : kept ? "Save again" : "Save current terminals");
    $("keep-save").disabled = !!keepBusy || !running || (auto && !loose);
    $("keep-save").title = !running ? "No terminals are running" : auto && !kept ? "Nothing is saved yet"
      : auto && !loose ? "Everything that is saved is running: nothing to forget" : "";
    const atLogin = s.terminals ? s.terminals.at_login : null;        // null: this PC has no unit for it
    $("keep-login-row").hidden = atLogin == null;
    if (!loginBusy) $("keep-login").checked = !!atLogin;
    $("keep-login").disabled = loginBusy;
    $("keep-restore").disabled = !!keepBusy || !gone.length;
    $("keep-restore").title = !kept ? "Nothing is saved yet" : gone.length ? ""
      : stopped.length ? "Every saved terminal is open. Where its conversation is not running, close that terminal first: Restore then brings it back with the conversation."
        : "Every saved terminal is already running";
    const said = $("keep-said");
    said.hidden = !keepSaid;
    if (keepSaid) { setText(said, keepSaid[1]); setClass(said, "keep-said" + (keepSaid[0] ? "" : " bad")); }
    // In the Terminals card: one press, while something saved is not running.
    const quick = $("term-restore");
    quick.hidden = !canManage || !gone.length;
    quick.disabled = !!keepBusy;
    setText($("term-restore-word"), keepBusy === "restore" ? "Restoring…" : "Restore " + gone.length + " saved");
    if (keepBusy === "restore") quick.hidden = !canManage;        // it stays, greyed out, while that is going on
    quick.title = gone.length ? "Start again: " + few(gone) : "";
  }
  async function keepDo(what, from) {
    if (keepBusy) return;
    keepBusy = what;
    keepAsk = false;
    clearTimeout(keepTimer);
    keepSaid = [true, what === "save" ? "Saving…" : "Starting the saved terminals…", from];
    if (snap) render();
    const done = await terminalService(["--" + what], 20000);
    try { await fetch("/api/snapshot?rescan=1", { cache: "no-store" }); } catch (e) { /* the next refresh shows it */ }
    keepSaid = [done.ok, done.ok ? done.text.replace(/\.?$/, ".") : done.text, from];
    if (snap) render();                         // the answer shows at once...
    // ...but the buttons stay off until the page has seen what was done. Until then it still lists the terminals
    // as not running, and a second press of Restore would be told, wrongly, that nothing could be started.
    const pause = (ms) => new Promise((go) => setTimeout(go, ms));
    await pause(700);
    await poll();
    await pause(1300);
    await poll();
    keepBusy = "";
    if (from === "card") keepTimer = setTimeout(() => { keepSaid = null; if (snap) render(); }, 15000);      // then the note leaves
    if (snap) render();
    setTimeout(poll, 4000);                     // a conversation takes a few seconds to come up in its terminal
  }
  $("keep-save").addEventListener("click", () => {
    const kept = snap && snap.terminals && snap.terminals.saved;
    const stopped = kept ? savedButStopped(snap) : [];
    if (kept && (kept.gone.length || stopped.length) && !keepAsk) {         // saving now would let go of something: say what
      keepAsk = true;
      const auto = !!snap.terminals.auto;
      keepSaid = [false, (auto ? "This makes the saved list exactly the terminals as they are right now."
        : "Saving replaces what is saved with the terminals as they are right now.")
        + (kept.gone.length ? " These saved ones are not running and would be forgotten: " + kept.gone.join(", ") + ". Restore them first to keep them." : "")
        + (stopped.length ? " These are open without the conversation saved with them, and would be saved as empty terminals: " + stopped.join(", ") + "." : "")
        + (auto ? " Or press Forget them." : " Or press Save anyway."), "settings"];
      return render();
    }
    keepDo("save", "settings");
  });
  $("keep-login").addEventListener("change", async (e) => {
    const on = e.target.checked;
    loginBusy = true;
    e.target.disabled = true;
    const done = await terminalService(["--at-login", on ? "on" : "off"]);
    try { await fetch("/api/snapshot?rescan=1", { cache: "no-store" }); } catch (err) { /* the next refresh shows it */ }
    keepSaid = done.ok ? null : [false, done.text, "settings"];
    // The box keeps what was chosen until the monitor reports it (or, if it never does, for a few seconds).
    for (let i = 0; done.ok && i < 6 && !(snap && snap.terminals.at_login === on); i++) {
      await new Promise((go) => setTimeout(go, 500));
      await poll();
    }
    loginBusy = false;
    if (snap) render();
  });
  $("keep-restore").addEventListener("click", () => keepDo("restore", "settings"));
  $("term-restore").addEventListener("click", () => keepDo("restore", "card"));
  $("settings").addEventListener("toggle", (e) => {     // closing Settings: the question and the last answer are done with
    if (e.newState === "open" || keepBusy || (keepSaid && keepSaid[2] === "card")) return;
    keepAsk = false;
    keepSaid = null;
  });

  let hintBuilt = false;
  function renderTerminals(s) {
    const t = s.terminals || { view: "off", type: "off", sessions: [] };
    const { canType, canWatch, canManage } = terminalRights(s);
    const card = $("terms-card");
    card.hidden = !(canType || canWatch);           // this device may not use terminals: no section at all
    if (card.hidden) return;
    $("term-new").hidden = !canManage;                 // whoever may type may also start, rename and close
    renderKeep(s, canManage);
    const list = t.sessions || [];
    $("terms-empty").hidden = list.length > 0;
    if (!list.length && !hintBuilt) {
      hintBuilt = true;
      $("terms-empty").appendChild(commandBox("tmux new -s work"));
    }
    const said = keepSaid && keepSaid[2] === "card" ? keepSaid[1] : "";       // the answer to "Restore" pressed in this card
    setText($("terms-note"), said || (!list.length ? "" : !t.live && me.type === 200
      ? "typing is off: run ./install-terminal.sh in the PC-health folder" : ""));
    $("terms-note").classList.toggle("say", !!said);
    const agents = new Map(s.agents.map((a) => [a.id, a]));
    // One compact row per terminal. First line: its name and its status, as the pill the Agents table uses.
    // Second line: what is in it (the conversation and the folder it works in). Then the ways in.
    // Renaming and closing are one step away, in the summary, for the terminals this device may manage.
    sync($("terms"), list, (x) => x.name, () => {
      const c = el("article", "term-row");
      const body = c._body = el("a", "term-body");             // the row itself leads to the view-only page
      const top = el("div", "term-top");
      const id = el("span", "term-id");                     // the name and, if it is saved, its bookmark: kept together
      c._name = el("span", "term-name");
      c._saved = savedMark();
      id.append(c._name, c._saved);
      c._pill = statePill("none", "", "");
      top.append(id, c._pill);
      const what = el("div", "term-what");
      c._title = el("span", "term-title");
      c._folder = el("span", "term-folder");
      what.append(c._title, c._folder);
      body.append(top, what);
      c._actions = el("div", "term-actions");
      c.append(body, c._actions);
      return c;
    }, (c, x) => {
      const a = x.agent ? agents.get(x.agent) : null;
      const state = a ? a.state : "none";
      setClass(c, "term-row" + (state === "waiting" ? " attention" : ""));
      setText(c._name, x.name);
      setClass(c._pill, "state st-" + state);
      setText(c._pill._word, a ? STATE_WORD[a.state] || "Running" : "No agent");
      setText(c._pill._since, a && a.state !== "working" && a.state_since ? dur(s.ts - a.state_since) : "");
      // The agent's project is usually the terminal's own name, so the conversation's title says more.
      const inside = a ? a.title || (a.label !== x.name ? a.label : "") : "running " + (x.cmd || "a shell");
      setText(c._title, [x.emdash ? "Emdash" : "", inside].filter(Boolean).join(" · "));
      setText(c._folder, a ? a.folder || "" : "");
      setSavedMark(c._saved, x.saved, s);
      c.title = sessionInfo(x);
      const sig = [x.openable, canType, canWatch, canManage, x.emdash].join("|");
      if (c._sig === sig) return;
      c._sig = sig;
      if (x.openable && canWatch) {
        c._body.href = watchUrl(x.name);
        c._body.setAttribute("aria-label", "View " + x.name + " without typing");
      } else c._body.removeAttribute("href");
      const bits = [];
      if (x.openable && canWatch) bits.push(wayIn("view", x.name, !canType));
      if (x.openable && canType) bits.push(wayIn("type", x.name, true));
      const info = el("button", "btn small sym");
      info.type = "button";
      info.title = "About " + x.name + ": summary" + (canManage && !x.emdash && x.openable ? ", rename, close" : "");
      info.setAttribute("aria-label", info.title);
      info.appendChild(glyph("info"));
      info.addEventListener("click", () => sheetShow(sessionJob("info", x.name)));
      bits.push(info);
      c._actions.replaceChildren(...bits);
    });
  }

  // ---------------------------------------------------------------- leftovers

  function renderLeftovers(s) {
    const list = s.leftovers || [];
    $("leftovers-empty").hidden = list.length > 0;
    $("leftovers-card").classList.toggle("is-empty", list.length === 0);
    setText($("leftovers-note"), list.length ? "PC Health never stops anything itself: copy the command and run it in a terminal" : "");
    sync($("leftovers"), list, (l) => l.id, () => {
      const row = el("div", "leftover");
      row._slot = el("span", "icon-slot");
      const body = el("div");
      row._what = el("div", "what");
      row._from = el("div", "facts");
      row._use = el("div", "facts");
      body.append(row._what, row._from, row._use);
      row._cmd = commandBox("");
      row.append(row._slot, body, row._cmd);
      return row;
    }, (row, l) => {
      setIcon(row._slot, l.level);
      setText(row._what, l.what + (l.n_procs > 1 ? " (" + l.n_procs + " processes)" : ""));
      const origin = l.from || "an agent session";
      setText(row._from, l.kind === "orphan"
        ? "Started by " + origin + (l.from_ended ? ", which ended " + longDur(s.ts - l.from_ended) + " ago." : ", which has ended.")
        : l.kind === "detached" ? "Started by " + origin + ", which has been idle for a long time."
          : "Looks like something an agent left running. It is not certain which one.");
      const use = ["Running for " + longDur(l.age_s)];
      if (l.cpu_avg_cores >= 0.05) use.push("about " + cores(l.cpu_avg_cores));
      use.push(bytes(l.mem_b) + " of memory");
      if (l.vram_b > MB) use.push(bytes(l.vram_b) + " of graphics memory");
      if (l.ports.length) use.push("listening on port " + l.ports.join(", "));
      setText(row._use, use.join(" · "));
      setText(row._cmd._code, l.cmd);
    });
  }

  // ---------------------------------------------------------------- bar lists

  function barList(host, items, opts) {
    const max = Math.max(opts.max || 0, ...items.map(opts.value), 1e-9);
    sync(host, items, opts.key, () => {
      const row = el("div", "bar");
      row._name = el("span", "name");
      row._val = el("span", "val");
      row._track = el("div", "track");
      row._fill = el("i");
      row._track.appendChild(row._fill);
      row.append(row._name, row._val, row._track);
      return row;
    }, (row, it) => {
      const sub = opts.sub ? opts.sub(it) : "";
      const sig = opts.name(it) + "|" + sub;
      if (row._sig !== sig) {
        row._sig = sig;
        row._name.replaceChildren(document.createTextNode(opts.name(it)), ...(sub ? [el("span", "muted", sub)] : []));
      }
      setText(row._val, opts.text(it));
      setWidth(row._fill, (100 * opts.value(it)) / max);
      const lv = opts.level ? opts.level(it) : "";
      setClass(row._track, "track" + (lv ? " lv-" + lv : ""));
      row.title = opts.title ? opts.title(it) : opts.name(it) + ": " + opts.text(it);
    });
  }

  function renderDetails(s) {
    const many = (p) => (p.n > 1 ? "×" + p.n : "");
    barList($("top-cpu"), s.top.cpu.filter((p) => p.cpu_cores >= 0.05).slice(0, 6), {
      key: (p) => p.name, name: (p) => p.name, sub: many, value: (p) => p.cpu_cores, text: (p) => cores(p.cpu_cores),
      title: (p) => p.name + ": " + p.n + (p.n > 1 ? " processes" : " process") + ", " + cores(p.cpu_cores) + ", " + bytes(p.mem_b),
    });
    barList($("top-mem"), s.top.mem.slice(0, 6), {
      key: (p) => p.name, name: (p) => p.name, sub: many, value: (p) => p.mem_b, text: (p) => bytes(p.mem_b),
      title: (p) => p.name + ": " + p.n + (p.n > 1 ? " processes" : " process") + ", " + bytes(p.mem_b),
    });
    barList($("temps"), s.temps, {
      key: (t) => t.id, name: (t) => t.label, value: (t) => t.c, text: (t) => Math.round(t.c) + " °C",
      max: Math.max(100, ...s.temps.map((t) => t.crit_c || 0)),
      level: (t) => (t.crit_c && t.c >= t.crit_c ? "crit" : t.warn_c && t.c >= t.warn_c ? "warn" : ""),
      title: (t) => t.label + ": " + t.c + " °C" + (t.warn_c ? " (warning from " + t.warn_c + " °C)" : ""),
    });
    sync($("fans"), s.fans, (f) => f.id, () => el("span", "chip"), (c, f) => setText(c, f.label + " " + f.rpm + " rpm"));

    const fo = s.folders || { items: [] };
    setText($("folders-note"), fo.scanning ? "measuring now…" : fo.age_s == null ? "first measurement starts shortly"
      : "measured " + dur(fo.age_s) + " ago");
    barList($("folders"), fo.items, {
      key: (f) => f.path, name: (f) => f.path, value: (f) => f.b,
      sub: (f) => (f.change_b && Math.abs(f.change_b) >= 0.1 * GB ? signedBytes(f.change_b) + " in " + dur(f.over_s) : "")
        + (f.main ? "" : " (not on the main disk)"),
      text: (f) => (f.partial ? "at least " : "") + bytes(f.b),
    });
    const sc = s.scratch || [];
    $("scratch-head").hidden = sc.length === 0;
    barList($("scratch"), sc, {
      key: (x) => x.label, name: (x) => x.label, sub: (x) => (x.live ? "" : "session ended"), value: (x) => x.b,
      text: (x) => (x.partial ? "at least " : "") + bytes(x.b),
    });
    const cr = (s.crash && s.crash.items) || [];
    $("crash-head").hidden = cr.length === 0;
    sync($("crash"), cr.slice(0, 6), (c) => c.program + ":" + c.ts, () => el("li"), (li, c) => {
      setText(li, c.program + " · " + when(c.ts) + (c.core ? " · left a " + bytes(c.b) + " crash dump" : ""));
    });
  }

  // ---------------------------------------------------------------- events

  let seenBefore = 0;
  try { seenBefore = Number(localStorage.getItem("pch-seen")) || 0; } catch (e) { /* private mode */ }
  function renderEvents(s) {
    const ev = s.events || [];
    $("events-empty").hidden = ev.length > 0;
    sync($("events"), ev.slice(0, 14), (e) => e.id + ":" + e.ts, () => {
      const li = el("li", "event");
      li._slot = el("span", "icon-slot");
      const body = el("div");
      li._msg = el("p");
      const meta = el("div", "when");
      li._when = el("span");
      li._new = el("span", "new-badge", "New");
      li._new.title = "New since you last looked";
      meta.append(li._when, li._new);
      body.append(li._msg, meta);
      li.append(li._slot, body);
      return li;
    }, (li, e) => {
      setIcon(li._slot, e.level === "crit" || e.level === "warn" ? e.level : "info");
      setText(li._msg, e.msg);
      let w = when(e.ts);
      if (e.end == null) w += " · still going";
      else if (e.end - e.ts >= 120) w += " · lasted " + longDur(e.end - e.ts);
      setText(li._when, w);
      li._new.hidden = !(seenBefore && e.ts > seenBefore);
    });
  }

  // ---------------------------------------------------------------- phone dock: jump between sections

  const dock = $("dock");
  const dockItems = Array.from(dock.querySelectorAll("button"));
  // Which dock item a section belongs to, in page order.
  const SPY = { banner: "top", tiles: "top", "agents-card": "agents-card", "terms-card": "terms-card",
    "disks-card": "disks-card", more: "more" };
  let dockNow = "top", dockHold = 0, atEnd = false;

  function dockMark(go) {
    dockNow = go;
    const shown = dockItems.filter((b) => !b.hidden);
    for (const b of dockItems) b.setAttribute("aria-current", String(b.dataset.go === go));
    dock.style.setProperty("--n", String(shown.length));                  // the lens is one item wide
    dock.style.setProperty("--i", String(Math.max(0, shown.findIndex((b) => b.dataset.go === go))));
  }
  for (const b of dockItems) {
    b.addEventListener("click", () => {
      const go = b.dataset.go;
      dockHold = Date.now() + 900;            // while the page glides there, scrolling must not drag the lens back
      dockMark(go);
      const behavior = still() ? "auto" : "smooth";
      if (go === "top") window.scrollTo({ top: 0, behavior });
      else $(go).scrollIntoView({ behavior, block: "start" });            // never the address hash: #details and #watch use it
    });
  }
  /** The current section is the last one whose top has passed a line 30% down the screen. */
  function dockSpy() {
    if (Date.now() < dockHold) return;
    let go = "top";
    if (atEnd) go = "more";                   // the last sections are short and never reach that line
    else {
      const line = innerHeight * 0.3;
      for (const id of Object.keys(SPY)) {
        const box = $(id).getBoundingClientRect();
        if (box.height && box.top <= line) go = SPY[id];
      }
    }
    if (go !== dockNow) dockMark(go);
  }
  if ("IntersectionObserver" in window) {
    // No scroll listener: these fire only when a section crosses that line, or the page end comes into view.
    const spy = new IntersectionObserver(dockSpy, { rootMargin: "-25% 0px -70% 0px" });
    for (const id of Object.keys(SPY)) spy.observe($(id));
    new IntersectionObserver((entries) => { atEnd = entries[0].isIntersecting; dockSpy(); }).observe($("footer"));
  }
  function renderDock(s) {
    const waiting = s.agent_counts.waiting;
    const badge = $("dock-badge");
    setText(badge, waiting || "");
    badge.hidden = !waiting;
    badge.parentNode.setAttribute("aria-label", waiting ? "Agents, " + waiting + " waiting for you" : "Agents");
    const hide = $("terms-card").hidden;      // this device may not use terminals: no dock item either
    if ($("dock-terms").hidden !== hide) { $("dock-terms").hidden = hide; dockMark(dockNow === "terms-card" && hide ? "top" : dockNow); }
  }

  function renderFooter(s) {
    const me = s.self;
    const bad = Object.entries(me.sources || {}).filter(([, v]) => v !== "ok" && v !== "none running");
    const problems = (bad.length ? " Not available: " + bad.map(([k, v]) => k + " (" + v + ")").join(", ") + "." : "")
      + (me.config_error ? " Settings file ignored: " + me.config_error : "");
    setText($("foot-short"), "PC Health " + me.version + " · " + bytes(me.rss_b) + " · " + me.cpu_pct.toFixed(1) + "% of one thread." + problems);
    setText($("foot-long"), "PC Health " + me.version + " is using " + bytes(me.rss_b) + " of memory and "
      + me.cpu_pct.toFixed(1) + "% of one processor thread. It refreshes every " + s.interval_s + " s while this page is open"
      + (me.throttled ? " (slowed down to stay light)" : "") + ". PC Health itself only reads; typing happens in your tmux sessions."
      + problems);
    setText($("host"), s.host + " · up " + longDur(s.uptime_s));
  }

  // ---------------------------------------------------------------- polling

  let snap = null, hist = null, range = "1h", lastOk = 0, pollTimer = null, histTimer = null, busy = false;

  function render() {
    if (!snap) return;
    renderBanner(snap);
    renderTerminals(snap);
    renderTiles(snap);
    renderDisks(snap);
    renderAgents(snap);
    renderLeftovers(snap);
    renderDetails(snap);
    renderEvents(snap);
    renderFooter(snap);
    renderDock(snap);
  }

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch("/api/snapshot", { cache: "no-store" });
      if (r.status === 401) { location.reload(); return; }
      if (r.ok) {
        const first = !snap;
        snap = await r.json();
        lastOk = Date.now();
        render();
        if (first) {
          document.body.classList.replace("loading", "ready");        // the cards rise in, once
          setTimeout(() => document.body.classList.remove("ready"), 1500);
          settled = true;
          loadHistory();
        }
      }
    } catch (e) { /* the freshness check below reports it */ } finally { busy = false; }
    freshness();
  }

  async function loadHistory() {
    const want = range;
    for (const t of Object.values(TILES)) t.spark.root.classList.toggle("loading", !hist || hist.range !== want);
    try {
      const r = await fetch("/api/history?range=" + want, { cache: "no-store" });
      if (r.ok && want === range) {
        hist = await r.json();
        if (snap) renderSparks(hist, snap);
      }
    } catch (e) { /* keep the previous trend on screen */ }
    for (const t of Object.values(TILES)) t.spark.root.classList.remove("loading");
  }

  function freshness() {
    const age = lastOk ? (Date.now() - lastOk) / 1000 : 0;
    const stale = lastOk > 0 && age > 10;
    document.body.classList.toggle("stale", stale);
    $("live").classList.toggle("off", !lastOk || stale);
    setText($("updated"), !lastOk ? "Connecting…" : stale ? "No answer from PC Health for " + dur(age) : "Updated " + clock(lastOk / 1000, true));
    if (stale) document.title = "? No data · PC Health";
  }

  function start() {
    stop();
    if (document.hidden) return;            // a hidden tab stops asking, so the monitor can slow down
    poll();
    if (snap) loadHistory();
    pollTimer = setInterval(poll, POLL_MS);
    histTimer = setInterval(loadHistory, HISTORY_MS[range]);
  }
  function stop() {
    clearInterval(pollTimer);
    clearInterval(histTimer);
  }

  for (const b of document.querySelectorAll(".ranges button")) {
    b.addEventListener("click", () => {
      range = b.dataset.range;
      const all = Array.from(document.querySelectorAll(".ranges button"));
      for (const o of all) o.setAttribute("aria-pressed", String(o === b));
      b.parentNode.style.setProperty("--i", String(all.indexOf(b)));       // the thumb slides to it
      start();
    });
  }
  // Phones: the four detail sections start folded, so the page is short. A tap on a heading opens one.
  if (matchMedia("(max-width: 640px)").matches && !SHOW_ALL) {
    for (const d of document.querySelectorAll("details.fold")) d.removeAttribute("open");
  }
  // Light follows the mouse across the panel under it. Only while the mouse moves; nothing runs at rest.
  if (matchMedia("(hover: hover) and (pointer: fine)").matches && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    let lit = null, last = null;
    const place = () => {
      const e = last;
      last = null;
      const panel = e.target instanceof Element ? e.target.closest(".card, .tile, .banner, .glass") : null;
      if (lit && lit !== panel) lit.classList.remove("lit");
      lit = panel;
      if (!panel) return;
      const r = panel.getBoundingClientRect();
      panel.style.setProperty("--mx", Math.round(e.clientX - r.left) + "px");
      panel.style.setProperty("--my", Math.round(e.clientY - r.top) + "px");
      panel.classList.add("lit");
    };
    document.addEventListener("pointermove", (e) => {
      if (!glassLook()) return;               // the Simple look has no highlight to move
      if (!last) requestAnimationFrame(place);
      last = e;
    }, { passive: true });
    document.documentElement.addEventListener("pointerleave", () => { if (lit) lit.classList.remove("lit"); lit = null; });
  }
  // Settings: which look this device uses. Switching takes effect at once and is remembered here only.
  const settings = $("settings");
  for (const input of settings.querySelectorAll('input[name="look"]')) {
    input.checked = input.value === (glassLook() ? "glass" : "simple");
    input.addEventListener("change", () => {
      if (!input.checked) return;
      root.dataset.look = input.value;
      try { localStorage.setItem("pch-look", input.value); } catch (e) { /* private mode: lasts until the page is closed */ }
      paintBrowserBar();
    });
  }
  if (!("popover" in HTMLElement.prototype)) {       // an older browser: plain show and hide
    settings.hidden = true;
    $("settings-btn").addEventListener("click", () => { settings.hidden = !settings.hidden; });
  }
  // The header sits lighter at the top of the page and gains weight once content slides under it.
  if ("IntersectionObserver" in window) {
    new IntersectionObserver((entries) => $("top").classList.toggle("scrolled", !entries[0].isIntersecting)).observe($("top-mark"));
  }
  document.addEventListener("visibilitychange", start);
  setInterval(freshness, 1000);
  loadMe();
  setInterval(loadMe, 60000);
  setTimeout(() => { try { localStorage.setItem("pch-seen", String(Math.floor(Date.now() / 1000))); } catch (e) { /* ignore */ } }, 15000);
  start();
})();

"use strict";
// One tmux session behind one bar: a tab per session with a mark for its agent's state (nav.js draws them; the
// tab of this session also says the state in words), the two ways of opening it, text size. Reached in one of two ways:
//   /terminal   open: the session itself, through the terminal service (ttyd) that PC Health passes along
//               under /tty. You can type; a phone gets the keys its keyboard lacks.
//   /watch      view only: a copy of the session's screen, read again every second. Nothing is ever sent to
//               the session. For Claude Code, earlier turns of the conversation can be loaded above the
//               screen from the session's own record, because its full-screen display leaves no scrollback.
(() => {
  const $ = (id) => document.getElementById(id);
  const VIEW = location.pathname === "/watch";
  const frame = $("tp-frame");
  const params = new URLSearchParams(location.search);
  const NAME_OK = /^[A-Za-z0-9_.-]{1,64}$/;
  let session = params.get("s") || "";
  const JUST_MADE = params.get("new") === "1" ? Date.now() : 0;      // the main page has just created this session
  const KEYS = { esc: "\x1b", tab: "\t", "shift-tab": "\x1b[Z", enter: "\r", "ctrl-c": "\x03", "ctrl-o": "\x0f", "ctrl-b": "\x02" };
  const ARROWS = { up: "A", down: "B", right: "C", left: "D" };
  const here = (name) => (VIEW ? "/watch" : "/terminal") + "?s=" + encodeURIComponent(name);
  const pad = (n) => String(n).padStart(2, "0");

  document.body.classList.toggle("tp-viewing", VIEW);
  $("tp-viewnote").hidden = !VIEW;
  $("tp-keys-toggle").hidden = VIEW;
  $(VIEW ? "tp-mode-view" : "tp-mode-type").setAttribute("aria-current", "page");       // the switch shows where you are

  /** The switch between the two modes: the other one is a link to the same session, or greyed out with the reason. */
  function showModes(name, me, live) {
    const may = (status) => status === 200 || status === 401;              // 401: the page there asks for the code
    const sides = [
      ["tp-mode-view", "/watch", VIEW, may(me.watch), "View only: watch without typing", "Viewing terminals is not allowed from this device."],
      ["tp-mode-type", "/terminal", !VIEW, may(me.type) && live, "Type: open the session itself",
        may(me.type) ? "The terminal service is not running on the PC." : "Typing is not allowed from this device."]];
    for (const [id, path, current, allowed, title, why] of sides) {
      const a = $(id);
      a.title = current ? title + " (you are here)" : allowed ? title : why;
      if (current || !allowed) a.removeAttribute("href");
      else a.href = path + (name ? "?s=" + encodeURIComponent(name) : "");
      if (!current && !allowed) a.setAttribute("aria-disabled", "true");
      else a.removeAttribute("aria-disabled");
    }
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { /* private mode: settings just do not stick */ }
    return null;
  }
  /** A message in place of the terminal: optionally a command to run, or the access-code box. */
  function message(text, withCommand, askCode) {
    const box = $("tp-msg");
    box.hidden = !text;
    $("tp-code").hidden = !askCode;
    (VIEW ? $("tp-view") : frame).hidden = !!text;
    box.replaceChildren();
    if (!text) return;
    box.appendChild(el("p", null, text));
    if (withCommand) box.appendChild(el("code", null, withCommand));
  }

  // ---- the terminal inside the frame (same address, so it can be reached directly)
  function term() {
    try { return frame.contentWindow && frame.contentWindow.term; } catch (e) { return null; }
  }
  function send(seq) {
    const t = term();
    if (!t) return;
    if (typeof t.input === "function") t.input(seq, true);
    else if (t._core && t._core.coreService) t._core.coreService.triggerDataEvent(seq, true);
  }
  function sendKey(name) {
    if (ARROWS[name]) {
      const t = term();
      const app = t && t.modes && t.modes.applicationCursorKeysMode;
      send((app ? "\x1bO" : "\x1b[") + ARROWS[name]);
    } else if (KEYS[name]) send(KEYS[name]);
  }

  // ---- text size, remembered per device (one setting for typing, one for viewing)
  let fontSize = Number(store("pch-term-font")) || (Math.min(innerWidth, innerHeight) < 600 ? 12 : 14);
  function applyFont() {
    const t = term();
    if (!t || !t.options) return;
    if (t.options.fontSize !== fontSize) {
      t.options.fontSize = fontSize;
      try {
        if (typeof t.fit === "function") t.fit();                 // use the whole frame at the new size
        frame.contentWindow.dispatchEvent(new Event("resize"));
      } catch (e) { /* refits on the next resize */ }
    }
    document.body.dataset.inputApi = typeof t.input === "function" ? "input" : t._core && t._core.coreService ? "core" : "none";
  }
  function changeFont(delta) {
    fontSize = Math.max(8, Math.min(24, fontSize + delta));
    store("pch-term-font", String(fontSize));
    applyFont();
  }
  $("tp-smaller").addEventListener("click", () => (VIEW ? changeViewFont(-1) : changeFont(-1)));
  $("tp-bigger").addEventListener("click", () => (VIEW ? changeViewFont(1) : changeFont(1)));
  if (!VIEW) {
    // Apply as soon as the terminal exists, then keep checking: it restores its own size when it reconnects.
    const early = setInterval(() => { if (term()) { applyFont(); clearInterval(early); } }, 120);
    setInterval(applyFont, 1000);
  }

  // ---- on-screen keys. They must not take focus, or a phone would close its keyboard.
  const keys = $("tp-keys");
  let keysOn = store("pch-term-keys");
  keysOn = keysOn === null ? matchMedia("(pointer: coarse)").matches : keysOn === "1";
  if (params.get("keys") === "1") keysOn = true;       // e.g. a bookmark for a touch screen that reports a mouse
  function showKeys() {
    keys.hidden = !keysOn;
    $("tp-keys-toggle").setAttribute("aria-pressed", String(keysOn));
  }
  $("tp-keys-toggle").addEventListener("click", () => { keysOn = !keysOn; store("pch-term-keys", keysOn ? "1" : "0"); showKeys(); });
  let byPointer = false;
  keys.addEventListener("pointerdown", (e) => {
    const b = e.target.closest("button[data-key]");
    if (!b) return;
    e.preventDefault();
    byPointer = true;
    if (navigator.vibrate) navigator.vibrate(8);        // a short tick under the finger, where the phone can
    sendKey(b.dataset.key);
  });
  keys.addEventListener("click", (e) => {            // keyboard and assistive-technology activation
    const b = e.target.closest("button[data-key]");
    if (b && !byPointer) sendKey(b.dataset.key);
    byPointer = false;
  });
  if (!VIEW) showKeys();

  // ---- keep everything above a phone's on-screen keyboard
  function fitViewport() {
    const vv = window.visualViewport;
    document.documentElement.style.setProperty("--tp-h", Math.round(vv ? vv.height : innerHeight) + "px");
    if (vv && vv.offsetTop) window.scrollTo(0, 0);
  }
  if (window.visualViewport) {
    visualViewport.addEventListener("resize", fitViewport);
    visualViewport.addEventListener("scroll", fitViewport);
  }
  addEventListener("resize", fitViewport);
  fitViewport();

  // ---- view only: a copy of the screen (colours kept), read again every second
  const ANSI16 = ["#2e3436", "#e05252", "#6aaf2e", "#d0a51e", "#5c8fd6", "#a97bb5", "#30a8aa", "#d3d7cf",
    "#6c6f6a", "#ff6b6b", "#9be64a", "#fce94f", "#8ab4f0", "#c9a0d6", "#5fe0e0", "#f4f4f2"];
  const TERM_FG = "#e4e4e4", TERM_BG = "#16161a";
  function colour256(n) {
    if (n < 16) return ANSI16[n];
    if (n >= 232) { const v = 8 + 10 * (n - 232); return `rgb(${v},${v},${v})`; }
    const lv = [0, 95, 135, 175, 215, 255];
    n -= 16;
    return `rgb(${lv[Math.floor(n / 36)]},${lv[Math.floor(n / 6) % 6]},${lv[n % 6]})`;
  }
  /** Apply one "ESC [ ... m" colour/style sequence to the running style. */
  function applySgr(st, params) {
    const p = params === "" ? [0] : params.split(";").map((x) => parseInt(x, 10) || 0);
    for (let i = 0; i < p.length; i++) {
      const c = p[i];
      if (c === 0) Object.assign(st, { fg: null, bg: null, bold: false, dim: false, italic: false, under: false, inv: false });
      else if (c === 1) st.bold = true;
      else if (c === 2) st.dim = true;
      else if (c === 3) st.italic = true;
      else if (c === 4) st.under = true;
      else if (c === 7) st.inv = true;
      else if (c === 22) st.bold = st.dim = false;
      else if (c === 23) st.italic = false;
      else if (c === 24) st.under = false;
      else if (c === 27) st.inv = false;
      else if (c >= 30 && c <= 37) st.fg = ANSI16[c - 30];
      else if (c >= 90 && c <= 97) st.fg = ANSI16[c - 82];
      else if (c >= 40 && c <= 47) st.bg = ANSI16[c - 40];
      else if (c >= 100 && c <= 107) st.bg = ANSI16[c - 92];
      else if (c === 39) st.fg = null;
      else if (c === 49) st.bg = null;
      else if ((c === 38 || c === 48) && p[i + 1] === 5) { st[c === 38 ? "fg" : "bg"] = colour256(p[i + 2] & 255); i += 2; }
      else if ((c === 38 || c === 48) && p[i + 1] === 2) {
        st[c === 38 ? "fg" : "bg"] = `rgb(${p[i + 2] & 255},${p[i + 3] & 255},${p[i + 4] & 255})`;
        i += 4;
      }
    }
  }
  /** Terminal text with colour codes -> DOM. Text goes in as text; only colours become styles. */
  function renderAnsi(text) {
    const frag = document.createDocumentFragment();
    const st = { fg: null, bg: null, bold: false, dim: false, italic: false, under: false, inv: false };
    const re = /\x1b\[([0-9;]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;
    let pos = 0, m;
    const emit = (chunk) => {
      if (!chunk) return;
      const plain = !st.fg && !st.bg && !st.bold && !st.dim && !st.italic && !st.under && !st.inv;
      if (plain) { frag.appendChild(document.createTextNode(chunk)); return; }
      const span = document.createElement("span");
      span.textContent = chunk;
      const fg = st.inv ? (st.bg || TERM_BG) : st.fg, bg = st.inv ? (st.fg || TERM_FG) : st.bg;
      if (fg) span.style.color = fg;
      if (bg) span.style.backgroundColor = bg;
      if (st.bold) span.style.fontWeight = "700";
      if (st.dim) span.style.opacity = "0.6";
      if (st.italic) span.style.fontStyle = "italic";
      if (st.under) span.style.textDecoration = "underline";
      frag.appendChild(span);
    };
    while ((m = re.exec(text))) {
      emit(text.slice(pos, m.index));
      if (m[2] === "m") applySgr(st, m[1]);
      pos = re.lastIndex;
    }
    emit(text.slice(pos));
    return frag;
  }

  const shown = { last: null, cols: 0, busy: false, timer: 0, failed: false };
  let viewFont = Number(store("pch-view-font")) || 0;       // 0: whatever makes the session's width fit the window
  function clock(ts) {
    const d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function sizeScreen() {
    const pre = $("tp-screen");
    if (viewFont) { pre.style.fontSize = viewFont + "px"; return; }
    pre.style.fontSize = "13px";
    const probe = el("span", null, "0".repeat(40));
    pre.appendChild(probe);
    const perChar = probe.getBoundingClientRect().width / 40 / 13;      // width of one character per px of font size
    probe.remove();
    if (!perChar || !shown.cols) return;
    const room = $("tp-view").clientWidth - 26;
    pre.style.fontSize = Math.max(8, Math.min(14, room / (shown.cols * perChar))).toFixed(2) + "px";
  }
  function changeViewFont(delta) {
    const now = viewFont || Math.round(parseFloat(getComputedStyle($("tp-screen")).fontSize)) || 13;
    viewFont = Math.max(6, Math.min(24, now + delta));
    store("pch-view-font", String(viewFont));
    sizeScreen();
  }
  const atEnd = (box) => box.scrollHeight - box.scrollTop - box.clientHeight < 40;

  async function pollScreen() {
    if (!session || shown.busy || document.hidden) return;
    shown.busy = true;
    let problem = "", askCode = false;
    try {
      const r = await fetch("/api/terminal?session=" + encodeURIComponent(session), { cache: "no-store" });
      if (r.ok) {
        const d = await r.json();
        $("tp-live-label").textContent = "Live screen · " + d.w + "×" + d.h + " · read at " + clock(d.ts);
        if (d.text !== shown.last) {
          const box = $("tp-view");
          const follow = shown.last === null || atEnd(box);       // reading further up: do not pull the page down
          shown.last = d.text;
          shown.cols = d.w;
          $("tp-screen").replaceChildren(renderAnsi(d.text));
          sizeScreen();
          if (follow) box.scrollTop = box.scrollHeight;
        }
      } else if (r.status === 401) { problem = "This device needs the access code first."; askCode = true; }
      else if (r.status === 403) problem = "Terminal screens can only be viewed on the PC itself. To allow this device, set terminal_view = \"lan\" in the PC Health settings file.";
      else problem = "This tmux session is no longer running. Choose another one above.";
    } catch (e) { problem = "No answer from PC Health."; }
    shown.busy = false;
    if (problem) { shown.failed = true; message(problem, null, askCode); }
    else if (shown.failed) { shown.failed = false; message(""); }
  }
  addEventListener("resize", () => { if (VIEW && shown.last !== null) sizeScreen(); });

  // ---- view only: earlier turns of the conversation, loaded on request from the session's own record
  const talk = { sid: null, start: null, end: null, busy: false, timer: 0, empty: 0 };
  function when(at) {
    const d = new Date(at);
    if (isNaN(d)) return "";
    const hm = pad(d.getHours()) + ":" + pad(d.getMinutes());
    return d.toDateString() === new Date().toDateString() ? hm : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + hm;
  }
  function turn(item) {
    if (item.r === "did") return el("div", "turn did", item.t);            // something the agent did: one line
    const box = el("div", "turn " + (item.r === "you" ? "you" : "agent"));
    const at = item.at ? when(item.at) : "";
    box.append(el("div", "turn-who", (item.r === "you" ? "You" : "Agent") + (at ? " · " + at : "")), el("div", "turn-text", item.t));
    return box;
  }
  function talkNote(text) {
    $("tp-log-note").hidden = !text;
    $("tp-log-note").textContent = text;
  }
  /** which: "newest" (first look), "older" (further back), "newer" (what was said since). */
  async function loadTalk(which) {
    if (talk.busy || !session) return;
    talk.busy = true;
    const box = $("tp-view"), turns = $("tp-turns"), more = $("tp-earlier");
    let again = false;
    try {
      const where = which === "older" ? "&before=" + talk.start : which === "newer" ? "&after=" + talk.end : "";
      const r = await fetch("/api/conversation?session=" + encodeURIComponent(session) + where, { cache: "no-store" });
      if (!r.ok) {
        if (which !== "newer") { more.hidden = true; talkNote("There is no earlier conversation to read for this session."); }
        return;
      }
      const d = await r.json();
      if (which !== "newest" && d.sid !== talk.sid) { again = true; return; }       // a new conversation began here
      const nodes = d.items.map(turn);
      if (which === "newer") {
        const follow = atEnd(box);
        turns.append(...nodes);
        talk.end = d.end;
        if (follow) box.scrollTop = box.scrollHeight;
      } else {
        const fromEnd = box.scrollHeight - box.scrollTop;
        if (which === "newest") {
          turns.replaceChildren(...nodes);
          talk.sid = d.sid;
          talk.end = d.end;
        } else turns.prepend(...nodes);
        talk.start = d.start;
        more.textContent = "Load earlier";
        more.hidden = d.start === 0;
        talkNote(d.start === 0 ? "The conversation starts here." : "");
        box.scrollTop = box.scrollHeight - fromEnd;         // what was on screen stays where it was
        talk.empty = nodes.length ? 0 : talk.empty + 1;     // a stretch of the record with nothing to show:
        if (!nodes.length && d.start > 0 && talk.empty < 6) setTimeout(() => loadTalk("older"), 0);       // keep going
        if (!talk.timer) talk.timer = setInterval(() => { if (!document.hidden) loadTalk("newer"); }, 10000);
      }
    } catch (e) {
      if (which !== "newer") talkNote("No answer from PC Health.");
    } finally {
      talk.busy = false;
      if (again) { talk.sid = talk.start = talk.end = null; loadTalk("newest"); }
    }
  }
  $("tp-earlier").addEventListener("click", () => loadTalk(talk.sid ? "older" : "newest"));
  if (VIEW && "IntersectionObserver" in window) {
    // Scrolling up is enough: when the button at the top comes into view, the page before it is fetched.
    new IntersectionObserver((seen) => { if (seen[0].isIntersecting) loadTalk(talk.sid ? "older" : "newest"); },
      { root: $("tp-view") }).observe($("tp-earlier"));
  }
  if (VIEW) {
    // Nothing typed or clicked here goes anywhere. Say so at the moment someone tries.
    let nudge = 0;
    const remind = () => {
      $("tp-viewnote").classList.add("nudge");
      clearTimeout(nudge);
      nudge = setTimeout(() => $("tp-viewnote").classList.remove("nudge"), 900);
    };
    addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.key.length !== 1 || e.key === " ") return;      // shortcuts and scrolling stay free
      if (e.target instanceof Element && e.target.closest("input, .tabs-list, .tp-bar")) return;
      remind();
    });
    $("tp-screen").addEventListener("click", () => { if (!String(getSelection())) remind(); });
  }

  // ---- which session

  function open(name) {
    session = name;
    document.title = name + (VIEW ? " · View only" : " · Terminal");
    message("");
    if (VIEW) {
      if (!shown.timer) { pollScreen(); shown.timer = setInterval(pollScreen, 1000); }
      return;
    }
    const want = "/tty/?arg=" + encodeURIComponent(name);
    if (frame.getAttribute("src") !== want) frame.setAttribute("src", want);
  }

  async function refresh(first) {
    let snap = null, me = null;
    try {
      // Not peek=1: the tabs show what every agent is doing, so this page counts as someone watching
      // and the monitor keeps the states fresh.
      const [a, b] = await Promise.all([fetch("/api/snapshot", { cache: "no-store" }), fetch("/api/me", { cache: "no-store" })]);
      if (a.ok) snap = await a.json();
      if (b.ok) me = await b.json();
    } catch (e) { /* handled below */ }
    if (!snap || !me) {
      if (first) message("No answer from PC Health.");
      return;
    }
    const t = snap.terminals || { sessions: [] };
    const list = (t.sessions || []).filter((x) => x.openable);
    const agents = new Map(snap.agents.map((a) => [a.id, a]));
    const showCurrent = () => {           // the tabs, with this session's the raised one, and the switch between the two modes
      const cur = list.find((x) => x.name === session);
      const agent = cur && cur.agent ? agents.get(cur.agent) : null;
      if (window.pchTabs) window.pchTabs($("tp-tabs"), { sessions: list, agents, ts: snap.ts, href: here, current: cur ? session : "" });
      showModes(cur ? session : "", me, !!t.live);
      $("tp-log").hidden = !(VIEW && agent && agent.kind === "claude");
    };
    showCurrent();
    const may = VIEW ? me.watch : me.type;
    if (may === 403) {
      return message(VIEW ? "Terminal screens can only be viewed on the PC itself. To allow this device, set terminal_view = \"lan\" in the PC Health settings file."
        : "Terminals can only be opened on the PC itself. To allow this device, set terminal_type = \"lan\" in the PC Health settings file.");
    }
    if (may === 401) return message("This device needs the access code first.", null, true);
    if (may === 404) return message(VIEW ? "Watching terminals is switched off in the PC Health settings." : "Terminals are switched off in the PC Health settings.");
    if (!VIEW && !t.live) return message("The terminal service is not running. On the PC, in the PC-health folder, run:", "./install-terminal.sh");
    if (!list.length) return message("No tmux sessions are running. Start one on the PC, then it appears here:", "tmux new -s work");
    if (!session || !NAME_OK.test(session) || !list.some((x) => x.name === session)) {
      if (JUST_MADE && Date.now() - JUST_MADE < 15000 && NAME_OK.test(session)) {       // not on the list yet: it will be
        if (first) fetch("/api/snapshot?rescan=1", { cache: "no-store" }).catch(() => { /* it shows up on its own */ });
        return open(session);
      }
      if (first && session && NAME_OK.test(session)) return message("There is no tmux session named \"" + session + "\". Choose one above.");
      if (!first && session) return message("The session \"" + session + "\" has ended. Choose another one above.");
      session = list[0].name;
    }
    showCurrent();                         // again: the session may just have been chosen above
    if (first || !VIEW || !shown.failed) open(session);
  }
  refresh(true);
  setInterval(() => { if (!document.hidden) refresh(false); }, 3000);
})();

"use strict";
// The bar of terminals on the terminal page (/terminal and /watch): one tab per tmux session, in the order the
// main page lists its terminals (waiting for you first, then working, then idle). It is drawn from the snapshot
// that page already reads: this file asks the server for nothing.
//
// A tab is a mark for what the agent in it is doing, and the terminal's name. The tab of the terminal on screen
// also says the state in words. A terminal that has something for you since you last opened it gets a dot after
// its name: its agent finished (it was working, now it is idle) or it began to wait for you. The dot goes when you
// open that terminal, or when its agent is at work again. What each terminal was doing is remembered for this
// browser tab only (sessionStorage), so the dots are not lost while moving from one terminal to another.
//
// When the tabs do not all fit, the bar scrolls sideways. A terminal that wants you (it is waiting, or it has
// its dot) may then be out of view: a small button appears at that end of the bar, and pressing it brings that
// tab into view. The bar itself only ever scrolls to keep the terminal on screen in view.
(() => {
  const STATE_WORD = { working: "Working", waiting: "Waiting for you", idle: "Idle", unknown: "Running", none: "No agent" };
  const KEY = "pch-tabs";
  let seen = {};                      // name -> [state, since when it has news (ms; 0 = none), the state before that]
  let kept = "";
  function recall() {
    try {
      const text = sessionStorage.getItem(KEY) || "";
      const old = JSON.parse(text || "{}");
      if (old && typeof old === "object") { seen = old; kept = text; }
    } catch (e) { /* private mode, or something else wrote there: remembered on this page only */ }
  }
  recall();
  // A page the browser brings back with its Back button returns as it was left, remembering what it knew then.
  // What the pages visited since have noted is in the storage: take that.
  addEventListener("pageshow", (e) => { if (e.persisted) recall(); });
  function keep() {
    const now = JSON.stringify(seen);
    if (now === kept) return;
    kept = now;
    try { sessionStorage.setItem(KEY, now); } catch (e) { /* as above */ }
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function setText(e, t) { if (e.textContent !== t) e.textContent = t; }
  function dur(s) {
    s = Math.max(0, Math.round(s));
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    if (s < 36 * 3600) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " days";
  }
  /** Scroll the bar sideways, and only the bar, until a tab is fully in view, clear of the buttons at its ends. */
  function reveal(host, a) {
    const box = host.getBoundingClientRect(), r = a.getBoundingClientRect();
    if (r.left < box.left + 72) host.scrollLeft -= box.left + 72 - r.left;
    else if (r.right > box.right - 72) host.scrollLeft += r.right - (box.right - 72);
  }
  /** What each end of the bar shows. Where the tabs do not all fit, it fades out at the side that has more
   *  (app.css), so it is plain that it scrolls; and if a terminal that wants you is out of view on that side,
   *  the button there says so. */
  function edges(host) {
    const hidden = host.scrollWidth - host.clientWidth;
    host.classList.toggle("more-left", host.scrollLeft > 2);
    host.classList.toggle("more-right", hidden - host.scrollLeft > 2);
    const box = host.getBoundingClientRect(), out = { left: [], right: [] };
    for (const a of host.children) {
      const wants = a.classList.contains("st-waiting") || a.classList.contains("news");
      if (!wants || a.hasAttribute("aria-current")) continue;
      const middle = (a.getBoundingClientRect().left + a.getBoundingClientRect().right) / 2;
      if (middle < box.left + 16) out.left.push(a);
      else if (middle > box.right - 16) out.right.push(a);
    }
    for (const side of ["left", "right"]) {
      const b = host._more[side], list = out[side];
      if (b.hidden !== !list.length) b.hidden = !list.length;
      host.classList.toggle("hint-" + side, list.length > 0);            // the tabs are cleared from under the button
      if (!list.length) continue;
      b._to = side === "left" ? list[list.length - 1] : list[0];           // the nearest of them
      b.classList.toggle("waiting", list.some((a) => a.classList.contains("st-waiting")));
      const title = "Out of view: " + list.map((a) => a._name + (a.classList.contains("st-waiting") ? " (waiting for you)" : " (finished)")).join(", ");
      if (b.title !== title) { b.title = title; b.setAttribute("aria-label", title); }
    }
  }
  /** The button at one end of the bar: an arrow and a dot. Shown only while a terminal that wants you is out of
   *  view that way; pressing it brings the nearest one into view. */
  function hint(host, side) {
    const b = el("button", "tabs-more " + side);
    b.type = "button";
    b.hidden = true;
    const arrow = el("span", "tabs-more-arrow", side === "left" ? "\u2039" : "\u203a"), dot = el("i", "tabs-more-dot");
    b.append(...(side === "left" ? [arrow, dot] : [dot, arrow]));
    b.addEventListener("click", () => {
      if (b._to && b._to.isConnected) reveal(host, b._to);
      edges(host);
    });
    return b;
  }
  function make(name) {
    const a = el("a", "tab");
    a._name = name;
    a._state = el("span", "tab-state");
    a._word = el("span", "tab-word");
    a._for = el("span", "tab-for");
    a._state.append(a._word, a._for);
    a.append(el("span", "mark"), el("span", "tab-name", name), el("span", "tab-news"), a._state);
    return a;
  }

  /** Draw the tabs into `host`, in the order given.
   *  o.sessions  the snapshot's terminals.sessions that can be opened
   *  o.agents    Map of the snapshot's agents by id
   *  o.ts        the snapshot's time
   *  o.href      name -> where its tab leads
   *  o.current   the terminal this page shows, or "" */
  window.pchTabs = (host, o) => {
    if (!host._more) {
      host._more = { left: hint(host, "left"), right: hint(host, "right") };
      host.before(host._more.left);
      host.after(host._more.right);
      host.addEventListener("scroll", () => edges(host), { passive: true });
      addEventListener("resize", () => edges(host));
    }
    if (!o.sessions.length) {         // an answer with no sessions in it (tmux may just not have answered in time):
      host.replaceChildren();         // the bar empties, but what the tabs remember is kept for when they are back
      edges(host);
      return;
    }
    const have = new Map();
    for (const node of Array.from(host.children)) have.set(node._name, node);
    const next = {};
    let prev = null, here = null, order = "";
    for (const x of o.sessions) {
      const agent = x.agent ? o.agents.get(x.agent) : null;
      const state = agent ? agent.state : "none";
      const current = x.name === o.current;
      const was = seen[x.name];
      let news = was ? was[1] : 0, before = was ? was[2] : "";
      if (was && was[0] !== state) {
        // It has something for you when it stops working or starts waiting. Back at work, that is no longer the latest.
        if (state === "waiting" || (was[0] === "working" && state === "idle")) { news = Date.now(); before = was[0]; }
        else if (state === "working") news = 0;
      }
      if (current) news = 0;                                // you are looking at it
      next[x.name] = [state, news, before];
      let a = have.get(x.name);
      if (a) have.delete(x.name); else a = make(x.name);
      const cls = "tab st-" + state + (news ? " news" : "");
      if (a.className !== cls) a.className = cls;
      const href = current ? null : o.href(x.name);        // the tab of the terminal on screen leads nowhere
      if (a.getAttribute("href") !== href) { if (href) a.setAttribute("href", href); else a.removeAttribute("href"); }
      if (current) { a.setAttribute("aria-current", "page"); here = a; } else a.removeAttribute("aria-current");
      // In words, on the tab of the terminal on screen and in every tab's tooltip.
      const word = STATE_WORD[state] || "Running";
      const since = agent && state !== "working" && agent.state_since ? dur(o.ts - agent.state_since) : "";
      setText(a._word, current ? word : "");
      setText(a._for, current ? since : "");
      const said = (agent && agent.state_text) || "";
      const detail = said.startsWith(word) ? said.slice(word.length).replace(/^[:,]?\s*/, "") : "";
      const what = agent ? agent.title || agent.label || "" : "running " + (x.cmd || "a shell");
      const title = x.name + ": " + word + (detail ? " (" + detail + ")" : "") + (since ? " for " + since : "")
        + (what ? "\n" + what : "")
        + (news ? "\n" + (state === "waiting" ? "Began to wait for you" : "Finished") + " since you last opened it" : "");
      if (a.title !== title) a.title = title;
      const label = x.name + ", " + word + (news ? ", new since you last opened it" : "");
      if (a.getAttribute("aria-label") !== label) a.setAttribute("aria-label", label);
      const want = prev ? prev.nextSibling : host.firstChild;
      if (a !== want) host.insertBefore(a, want);
      prev = a;
      order += x.name + "\n";
    }
    for (const node of have.values()) node.remove();
    seen = next;
    keep();
    // Where the tabs do not all fit, the bar scrolls sideways by itself only to keep the terminal on screen in
    // view: when the page opens, and when the tabs change places. Not while a finger or the mouse is on the bar:
    // then you are the one scrolling it. A tab that wants you and is out of view gets the button at that end.
    if (here && (host._shown !== o.current || (host._order !== order && !host.parentNode.matches(":hover")))) reveal(host, here);
    host._shown = o.current;
    host._order = order;
    edges(host);
  };
})();

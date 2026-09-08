/* LLM Proxy — live conversation UI (vanilla JS, no build).
 *
 * One WebSocket to /ws drives live updates; REST (/api/...) is used for the
 * initial load, export, and clear. The dock shows conversations; the main pane
 * renders each exchange two-sided (client request | server response) with
 * live token streaming, timestamps, timing deltas, and expandable full wire.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    clients: [],
    focusCid: null,
    ws: null,
    wsRetry: null,
    exById: new Map(), // exchange_id -> exchange object (focused conversation)
    live: new Map(), // exchange_id -> accumulated streamed text
    lastSeq: 0,
    stick: true,
  };

  // ---------- formatting ----------
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  function fmtTime(epoch) {
    if (!epoch) return "—";
    const d = new Date(epoch * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }
  function fmtClock(epoch) {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function fmtMs(ms) {
    if (ms == null) return "—";
    return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`;
  }
  function toksPerSec(ex) {
    const t = ex.timings || {};
    const u = ex.usage || {};
    if (!u.completion_tokens) return null;
    // Prefer the upstream-reported rate (llama.cpp embeds exact generation timings).
    if (t.gen_tok_per_sec != null) return t.gen_tok_per_sec.toFixed(1);
    // Fallback (streams only): content-generation window. Not usable for
    // non-stream (first byte *is* the end) or reasoning-heavy streams.
    if (t.t_first_content != null && t.t_last_content != null && t.t_last_content > t.t_first_content) {
      return (u.completion_tokens / (t.t_last_content - t.t_first_content)).toFixed(1);
    }
    return null;
  }

  // ---------- api ----------
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  // ---------- dock ----------
  function dockRow(c) {
    const cid = (c.conversation_ids && c.conversation_ids[0]) || c.id;
    const li = document.createElement("li");
    li.className = "dock-row";
    li.dataset.cid = cid;
    li.innerHTML = `
      <div class="dr-top"><span class="dr-name"></span><span class="dr-time"></span></div>
      <div class="dr-sub"></div>`;
    li.querySelector(".dr-name").textContent = c.name;
    li.querySelector(".dr-sub").textContent = c.id;
    li.querySelector(".dr-time").textContent = fmtClock(c.last_seen);
    li.addEventListener("click", () => selectConversation(cid, c.name));
    return li;
  }

  function renderDock(filter = "") {
    const list = $("dock-list");
    list.innerHTML = "";
    const f = filter.toLowerCase();
    for (const c of state.clients) {
      if (f && !c.name.toLowerCase().includes(f) && !c.id.toLowerCase().includes(f)) continue;
      list.appendChild(dockRow(c));
    }
    $("dock-empty").style.display = state.clients.length ? "none" : "block";
    markFocus();
    $("stat-clients").textContent = `clients: ${state.clients.length}`;
  }

  function markFocus() {
    for (const li of $("dock-list").children) li.classList.toggle("focused", li.dataset.cid === state.focusCid);
  }

  function pulse(cid) {
    const li = [...$("dock-list").children].find((x) => x.dataset.cid === cid);
    if (!li) return;
    li.querySelector(".dr-time").textContent = "now";
    li.classList.remove("pulse");
    void li.offsetWidth; // restart the animation
    li.classList.add("pulse");
    clearTimeout(li._pt);
    li._pt = setTimeout(() => {
      li.classList.remove("pulse");
      const c = state.clients.find((x) => (x.conversation_ids && x.conversation_ids[0]) === cid);
      if (c) li.querySelector(".dr-time").textContent = fmtClock(c.last_seen);
    }, 1500);
  }

  async function refreshClients() {
    try {
      state.clients = await api("/api/clients");
      renderDock($("filter").value);
    } catch {
      /* ignore transient errors */
    }
  }

  // ---------- conversation ----------
  async function selectConversation(cid, name) {
    if (!cid) return;
    state.focusCid = cid;
    state.exById.clear();
    state.live.clear();
    state.lastSeq = 0;
    markFocus();
    if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify({ type: "subscribe", conversation_id: cid }));
    $("empty").hidden = true;
    $("conv").hidden = false;
    $("conv-title").textContent = name || cid;
    const list = $("ex-list");
    list.innerHTML = "";
    try {
      const conv = await api(`/api/conversations/${encodeURIComponent(cid)}`);
      for (const ex of conv.exchanges) upsertExchange(ex);
    } catch (e) {
      list.innerHTML = `<div class="err">failed to load: ${esc(e.message)}</div>`;
    }
    maybeScroll();
  }

  // ---------- exchange rendering ----------
  function previewOf(ex) {
    const cr = ex.client_request || {};
    if (cr.preview) return cr.preview;
    const body = cr.body_json || {};
    const msgs = body.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "user") {
        return typeof msgs[i].content === "string" ? msgs[i].content : JSON.stringify(msgs[i].content);
      }
    }
    return "";
  }

  function fullRequest(ex) {
    const cr = ex.client_request || {};
    return JSON.stringify({ method: cr.method, path: cr.path, headers: cr.headers, body: cr.body_json }, null, 2);
  }
  function fullResponse(ex) {
    const sr = ex.server_response || {};
    const obj = { status: sr.status, headers: sr.headers, streaming: sr.streaming, size_bytes: sr.size_bytes };
    if (sr.reassembled) obj.reassembled = sr.reassembled;
    if (sr.body_json) obj.body = sr.body_json;
    if (sr.chunks) obj.chunks = sr.chunks;
    return JSON.stringify(obj, null, 2);
  }
  function respText(ex) {
    const sr = ex.server_response || {};
    for (const key of ["reassembled", "body_json"]) {
      const c = ((sr[key] || {}).choices || [])[0];
      if (c && c.message && c.message.content != null) return c.message.content;
    }
    return "";
  }
  function reasoningText(ex) {
    const sr = ex.server_response || {};
    for (const key of ["reassembled", "body_json"]) {
      const c = ((sr[key] || {}).choices || [])[0];
      if (c && c.message && c.message.reasoning_content) return c.message.reasoning_content;
    }
    return "";
  }
  function timingsText(ex) {
    const t = ex.timings || {};
    const parts = [];
    if (t.ttft_ms != null) parts.push(`ttft ${fmtMs(t.ttft_ms)}`);
    if (t.total_ms != null) parts.push(`total ${fmtMs(t.total_ms)}`);
    const tps = toksPerSec(ex);
    if (tps) parts.push(`${tps} tok/s`);
    return parts.join(" · ");
  }

  function clientSideHtml(ex) {
    const cr = ex.client_request || {};
    const body = cr.body_json || {};
    const model = body.model || cr.model;
    const pv = previewOf(ex);
    return `
      <div class="side client">
        <div class="side-label">CLIENT</div>
        <div class="line">${esc(cr.method)} ${esc(cr.path)}</div>
        ${model ? `<div class="line model">model: ${esc(model)}</div>` : ""}
        ${pv ? '<div class="preview"></div>' : ""}
        <details class="full"><summary>full request</summary><pre class="req-pre"></pre></details>
      </div>`;
  }
  function serverSideHtml(ex, reasoning) {
    const sr = ex.server_response || {};
    const status = sr.status != null ? sr.status : "…";
    return `
      <div class="side server">
        <div class="side-label">SERVER</div>
        <div class="line">${status}${sr.streaming ? " · stream" : ""}</div>
        ${reasoning ? '<div class="resp-reasoning"></div>' : ""}
        <div class="resp-text"></div>
        ${ex.error ? `<div class="err">${esc(ex.error.message)}</div>` : ""}
        <details class="full"><summary>full response</summary><pre class="resp-pre"></pre></details>
      </div>`;
  }

  function placeCursor(el, st) {
    // One live cursor: on the content block once it has text, else on reasoning.
    const rt = el.querySelector(".resp-text");
    const rr = el.querySelector(".resp-reasoning");
    let host = null;
    if (st.content) host = rt;
    else if (rr) host = rr;
    if (!host) return;
    let cur = el.querySelector(".cursor");
    if (!cur) {
      cur = document.createElement("span");
      cur.className = "cursor";
    }
    if (cur.parentNode !== host) host.appendChild(cur);
  }

  function exchangeCard(ex, live) {
    const isLive = live != null;
    const text = isLive ? live.content : respText(ex);
    const reasoning = isLive ? live.reasoning : reasoningText(ex);
    const badges = [];
    if (ex.server_response && ex.server_response.streaming) badges.push('<span class="badge stream">stream</span>');
    if (ex.is_replay) badges.push('<span class="badge replay">replay</span>');
    if (ex.error) badges.push('<span class="badge err">error</span>');

    const el = document.createElement("div");
    el.className = "exchange" + (isLive ? " live" : "");
    el.dataset.exid = ex.id;
    el.innerHTML = `
      <div class="ex-head">
        <span class="ex-seq">#${ex.sequence}</span>
        <span class="ex-time">${fmtTime(ex.client_request && ex.client_request.timestamp)}</span>
        <span class="ex-timings"></span>
        ${badges.join("")}
      </div>
      <div class="ex-body">${clientSideHtml(ex)}${serverSideHtml(ex, reasoning)}</div>`;

    el.querySelector(".ex-timings").textContent = timingsText(ex);
    const pv = el.querySelector(".preview");
    if (pv) pv.textContent = previewOf(ex);
    el.querySelector(".req-pre").textContent = fullRequest(ex);
    el.querySelector(".resp-pre").textContent = fullResponse(ex);
    const rt = el.querySelector(".resp-text");
    rt.textContent = text;
    const rr = el.querySelector(".resp-reasoning");
    if (rr) rr.textContent = reasoning;
    if (isLive) placeCursor(el, { content: text, reasoning });
    return el;
  }

  function upsertExchange(ex) {
    const list = $("ex-list");
    const existing = list.querySelector(`.exchange[data-exid="${ex.id}"]`);
    const card = exchangeCard(ex, null);
    if (existing) existing.replaceWith(card);
    else list.appendChild(card);
    state.exById.set(ex.id, ex);
    state.live.delete(ex.id);
    if (typeof ex.sequence === "number" && ex.sequence > state.lastSeq) state.lastSeq = ex.sequence;
    maybeScroll();
  }

  function updateLive(exid) {
    const el = $("ex-list").querySelector(`.exchange[data-exid="${exid}"]`);
    if (!el) return;
    const st = state.live.get(exid) || { content: "", reasoning: "" };
    const rt = el.querySelector(".resp-text");
    if (rt) rt.textContent = st.content;
    let rr = el.querySelector(".resp-reasoning");
    if (st.reasoning && !rr) {
      // Reasoning started streaming after the card was built without one.
      rr = document.createElement("div");
      rr.className = "resp-reasoning";
      if (rt) rt.before(rr);
    }
    if (rr) rr.textContent = st.reasoning;
    placeCursor(el, st);
    maybeScroll();
  }

  function maybeScroll() {
    if (!state.stick) return;
    const list = $("ex-list");
    list.scrollTop = list.scrollHeight;
  }

  // ---------- websocket ----------
  function onWsMessage(ev) {
    switch (ev.type) {
      case "activity":
        pulse(ev.conversation_id);
        break;
      case "client_seen":
        refreshClients();
        break;
      case "exchange_started":
        if (ev.conversation_id === state.focusCid) {
          upsertExchange({
            id: ev.exchange_id,
            sequence: state.lastSeq + 1,
            is_replay: false,
            client_request: ev.client_request,
            server_response: { status: "…", streaming: ev.streaming },
            timings: {},
            usage: null,
          });
        }
        break;
      case "delta":
        if (ev.conversation_id === state.focusCid) {
          const st = state.live.get(ev.exchange_id) || { content: "", reasoning: "" };
          if (ev.delta) st.content += ev.delta;
          if (ev.reasoning_delta) st.reasoning += ev.reasoning_delta;
          state.live.set(ev.exchange_id, st);
          updateLive(ev.exchange_id);
        }
        break;
      case "exchange_completed":
        if (ev.conversation_id === state.focusCid) upsertExchange(ev.exchange);
        break;
    }
  }

  function setWs(on) {
    const p = $("stat-ws");
    p.textContent = `ws: ${on ? "on" : "off"}`;
    p.classList.toggle("ws-on", on);
    p.classList.toggle("ws-off", !on);
  }

  function connectWs() {
    if (state.wsRetry) {
      clearTimeout(state.wsRetry);
      state.wsRetry = null;
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    state.ws = ws;
    ws.onopen = () => {
      setWs(true);
      if (state.focusCid) ws.send(JSON.stringify({ type: "subscribe", conversation_id: state.focusCid }));
    };
    ws.onmessage = (m) => {
      try {
        onWsMessage(JSON.parse(m.data));
      } catch (e) {
        console.error("ws message handling failed", e, m.data);
      }
    };
    ws.onclose = (ev) => {
      if (state.ws !== ws) return; // superseded by a newer connection
      setWs(false);
      console.error(`ws closed (code=${ev.code} reason=${esc(ev.reason || "-")})`);
      state.wsRetry = setTimeout(connectWs, 2000);
    };
    ws.onerror = (ev) => {
      console.error("ws error", ev);
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    };
  }

  // ---------- misc ----------
  async function checkUpstream() {
    try {
      const h = await api("/health");
      $("stat-upstream").textContent = `upstream: ${h.status}`;
      $("stat-upstream").classList.add("ok");
    } catch {
      $("stat-upstream").textContent = "upstream: error";
    }
  }

  function wireButtons() {
    $("btn-export").addEventListener("click", () => {
      if (!state.focusCid) return;
      const a = document.createElement("a");
      a.href = `/api/conversations/${encodeURIComponent(state.focusCid)}/export`;
      a.download = `${state.focusCid}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    $("btn-clear").addEventListener("click", async () => {
      if (!state.focusCid) return;
      await fetch(`/api/conversations/${encodeURIComponent(state.focusCid)}`, { method: "DELETE" });
      $("ex-list").innerHTML = "";
      state.exById.clear();
      state.live.clear();
      state.lastSeq = 0;
    });
    $("stick").addEventListener("change", (e) => {
      state.stick = e.target.checked;
      if (state.stick) maybeScroll();
    });
    $("filter").addEventListener("input", (e) => renderDock(e.target.value));
  }

  async function init() {
    wireButtons();
    checkUpstream();
    await refreshClients();
    connectWs();
  }
  init();
})();

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
    wsAttempts: 0, // reconnect attempts since page load (diagnostics)
    wsOpenedAt: null, // ms; age of the current socket at close (diagnostics)
    wsLastMsgAt: null, // ms; last WS message received (stale detection)
    exById: new Map(), // exchange_id -> exchange object (focused conversation)
    live: new Map(), // exchange_id -> accumulated streamed text
    lastSeq: 0,
    stick: true,
    replaySeq: null, // sequence loaded into the replay dock
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
      <div class="dr-top">
        <span class="dr-name"></span><span class="dr-time"></span>
        <button class="dr-remove" title="Remove client" aria-label="Remove client">×</button>
      </div>
      <div class="dr-sub"></div>`;
    li.querySelector(".dr-name").textContent = c.name;
    li.querySelector(".dr-sub").textContent = c.id;
    li.querySelector(".dr-time").textContent = fmtClock(c.last_seen);
    li.addEventListener("click", () => selectConversation(cid, c.name));
    li.querySelector(".dr-remove").addEventListener("click", (e) => {
      e.stopPropagation();
      removeClient(c);
    });
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
    $("empty-msg").textContent = state.clients.length ? "Select a conversation on the left." : "No traffic yet.";
    markFocus();
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

  async function removeClient(c) {
    try {
      await api(`/api/clients/${encodeURIComponent(c.id)}`, { method: "DELETE" });
    } catch {
      return;
    }
    // If the focused conversation belonged to this client, reset the main pane
    // and drop the server-side focus.
    const cids = new Set(c.conversation_ids || []);
    cids.add(c.id);
    if (state.focusCid && cids.has(state.focusCid)) {
      state.focusCid = null;
      state.exById.clear();
      state.live.clear();
      state.lastSeq = 0;
      closeReplayDock();
      $("conv").hidden = true;
      $("empty").hidden = false;
      if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify({ type: "unsub" }));
    }
    await refreshClients();
  }

  // ---------- conversation ----------
  async function selectConversation(cid, name) {
    if (!cid) return;
    state.focusCid = cid;
    state.exById.clear();
    state.live.clear();
    state.lastSeq = 0;
    closeReplayDock();
    markFocus();
    if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify({ type: "subscribe", conversation_id: cid }));
    $("empty").hidden = true;
    $("conv").hidden = false;
    $("conv-title").textContent = name || cid;
    const list = $("ex-list");
    list.innerHTML = "";
    try {
      const conv = await api(`/api/conversations/${encodeURIComponent(cid)}`);
      for (const ex of conv.exchanges) {
        if (ex.in_flight) {
          // Pending card for a still-running exchange; WS deltas/completion
          // (we are now focused) update it in place.
          upsertExchange({ ...ex, server_response: { status: "…", streaming: !!ex.streaming } });
          if (state.live.has(ex.id)) updateLive(ex.id);
        } else {
          upsertExchange(ex);
        }
      }
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
    const obj = { method: cr.method, path: cr.path, headers: cr.headers, body: cr.body_json };
    if (cr.body_text != null) obj.body_text = cr.body_text;
    return JSON.stringify(obj, null, 2);
  }
  function fullResponse(ex) {
    const sr = ex.server_response || {};
    const obj = { status: sr.status, headers: sr.headers, streaming: sr.streaming, size_bytes: sr.size_bytes };
    if (sr.reassembled) obj.reassembled = sr.reassembled;
    if (sr.body_json) obj.body = sr.body_json;
    if (sr.body_text != null) obj.body_text = sr.body_text;
    if (sr.chunks) obj.chunks = sr.chunks;
    return JSON.stringify(obj, null, 2);
  }
  function respText(ex) {
    const sr = ex.server_response || {};
    for (const key of ["reassembled", "body_json"]) {
      const c = ((sr[key] || {}).choices || [])[0];
      if (c && c.message && c.message.content != null) return c.message.content;
    }
    if (sr.body_text != null) {
      const t = String(sr.body_text).trim();
      return t.length <= 200 ? t : t.slice(0, 200) + "…";
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
  function thinkSummary(text) {
    const t = String(text || "").trim();
    if (!t) return "thinking";
    const n = t.split(/\s+/).length;
    return `thinking · ${n} ${n === 1 ? "word" : "words"}`;
  }
  function wireThink(think) {
    // The thinking block follows streaming text by default. A manual scroll up
    // unsticks it for the life of the block; collapsing and reopening resticks.
    think._stick = true;
    const tt = think.querySelector(".think-text");
    tt.addEventListener("scroll", () => {
      if (tt.scrollHeight - tt.scrollTop - tt.clientHeight > 16) think._stick = false;
    });
    think.addEventListener("toggle", () => {
      if (think.open) {
        think._stick = true;
        tt.scrollTop = tt.scrollHeight;
      }
    });
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
    const think = reasoning
      ? '<details class="think"><summary class="think-summary"></summary><pre class="think-text"></pre></details>'
      : "";
    return `
      <div class="side server">
        <div class="side-label">SERVER</div>
        <div class="line">${status}${sr.streaming ? " · stream" : ""}</div>
        ${think}
        <div class="resp-text"></div>
        ${ex.error ? `<div class="err">${esc(ex.error.message)}</div>` : ""}
        <details class="full"><summary>full response</summary><pre class="resp-pre"></pre></details>
      </div>`;
  }

  function placeCursor(el, st) {
    // One live cursor: on the content block once it has text, else on the thinking block.
    const rt = el.querySelector(".resp-text");
    const tt = el.querySelector(".think-text");
    let host = null;
    if (st.content) host = rt;
    else if (tt) host = tt;
    if (!host) return;
    let cur = el.querySelector(".cursor");
    if (!cur) {
      cur = document.createElement("span");
      cur.className = "cursor";
    }
    if (cur.parentNode !== host) host.appendChild(cur);
  }

  // ---------- replay (dock) / per-exchange export ----------
  async function postReplay(cid, seq, body) {
    // Re-send a captured request, replacing its body with the full edited body
    // (an empty body replays as-is). The replay streams in like any other
    // exchange and is flagged is_replay.
    const r = await fetch(`/api/conversations/${encodeURIComponent(cid)}/exchanges/${seq}/replay`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      const err = await r.text();
      throw new Error(`${r.status} ${err}`);
    }
  }

  // Replay editor dock: the raw JSON body is the source of truth; the quick
  // fields mirror it for the common single-value edits and are disabled when the
  // body is not valid JSON.
  const RD_FIELDS = [
    { id: "rd-model", key: "model", type: "text" },
    { id: "rd-temperature", key: "temperature", type: "number" },
    { id: "rd-top_p", key: "top_p", type: "number" },
    { id: "rd-max_tokens", key: "max_tokens", type: "number" },
    { id: "rd-stream", key: "stream", type: "boolean" },
  ];

  function rdParse() {
    try {
      const o = JSON.parse($("rd-body").value);
      return o && typeof o === "object" && !Array.isArray(o) ? o : null;
    } catch {
      return null;
    }
  }
  function rdSetBody(obj) {
    $("rd-body").value = JSON.stringify(obj, null, 2);
  }
  function rdSyncFields() {
    const o = rdParse();
    for (const f of RD_FIELDS) {
      const el = $(f.id);
      el.disabled = o == null;
      if (o == null) {
        if (f.type === "boolean") el.checked = false;
        else el.value = "";
      } else if (f.type === "boolean") {
        el.checked = !!o[f.key];
      } else {
        el.value = o[f.key] == null ? "" : String(o[f.key]);
      }
    }
  }
  function rdPatch(key, value) {
    const o = rdParse();
    if (o == null) return;
    o[key] = value;
    rdSetBody(o);
  }
  function rdUnset(key) {
    const o = rdParse();
    if (o == null) return;
    delete o[key];
    rdSetBody(o);
  }
  function rdSetStatus(msg, isErr) {
    const el = $("rd-status");
    el.textContent = msg;
    el.classList.toggle("err", !!isErr);
  }
  function openReplayDock(ex) {
    if (!state.focusCid) return;
    state.replaySeq = ex.sequence;
    const body = (ex.client_request || {}).body_json;
    if (body && typeof body === "object") {
      rdSetBody(body);
      rdSetStatus("The body below is re-sent in place of the captured request. Cancel to discard.");
    } else {
      rdSetBody({});
      rdSetStatus("No editable JSON body was captured for this exchange.", true);
    }
    rdSyncFields();
    const model = ((ex.client_request || {}).body_json || {}).model;
    $("rd-sub").textContent = `#${ex.sequence}${model ? ` · ${model}` : ""}`;
    $("replay-dock").hidden = false;
    $("rd-body").focus();
  }
  function closeReplayDock() {
    state.replaySeq = null;
    $("replay-dock").hidden = true;
  }
  async function sendReplay() {
    const seq = state.replaySeq;
    if (seq == null || !state.focusCid) return;
    const o = rdParse();
    if (o == null) {
      rdSetStatus("Body is not valid JSON.", true);
      return;
    }
    $("rd-send").disabled = true;
    rdSetStatus("Sending…");
    try {
      await postReplay(state.focusCid, seq, o);
      closeReplayDock();
    } catch (e) {
      rdSetStatus(`replay failed: ${e.message}`, true);
    } finally {
      $("rd-send").disabled = false;
    }
  }
  function wireReplayDock() {
    $("rd-cancel").addEventListener("click", closeReplayDock);
    $("rd-send").addEventListener("click", sendReplay);
    $("rd-body").addEventListener("input", rdSyncFields);
    for (const f of RD_FIELDS) {
      $(f.id).addEventListener("input", (e) => {
        if (f.type === "boolean") rdPatch(f.key, e.target.checked);
        else if (e.target.value === "") rdUnset(f.key);
        else rdPatch(f.key, f.type === "number" ? Number(e.target.value) : e.target.value);
      });
    }
  }

  function exportExchange(cid, seq) {
    const a = document.createElement("a");
    a.href = `/api/conversations/${encodeURIComponent(cid)}/exchanges/${seq}/export`;
    a.download = `${cid}-ex${seq}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
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
        <span class="ex-actions">
          <button class="btn btn-sm" data-action="replay" title="Edit and re-send this request">replay</button>
          <button class="btn btn-sm" data-action="export" title="Download this exchange as JSON">export</button>
        </span>
      </div>
      <div class="ex-body">${clientSideHtml(ex)}${serverSideHtml(ex, reasoning)}</div>`;

    el.querySelector(".ex-timings").textContent = timingsText(ex);
    el.querySelector('[data-action="replay"]').addEventListener("click", () => openReplayDock(ex));
    el.querySelector('[data-action="export"]').addEventListener("click", () => exportExchange(state.focusCid, ex.sequence));
    const pv = el.querySelector(".preview");
    if (pv) pv.textContent = previewOf(ex);
    el.querySelector(".req-pre").textContent = fullRequest(ex);
    el.querySelector(".resp-pre").textContent = fullResponse(ex);
    const rt = el.querySelector(".resp-text");
    rt.textContent = text;
    const think = el.querySelector(".think");
    if (think) {
      wireThink(think);
      think.querySelector(".think-text").textContent = reasoning;
      think.querySelector(".think-summary").textContent = thinkSummary(reasoning);
    }
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
    // A completed exchange drops its live buffer (cursor); an in-flight one keeps
    // it, so deltas that raced ahead of the REST render are not lost.
    if (!ex.in_flight) state.live.delete(ex.id);
    if (typeof ex.sequence === "number" && ex.sequence > state.lastSeq) state.lastSeq = ex.sequence;
    maybeScroll();
  }

  function updateLive(exid) {
    const el = $("ex-list").querySelector(`.exchange[data-exid="${exid}"]`);
    if (!el) return;
    const st = state.live.get(exid) || { content: "", reasoning: "" };
    const rt = el.querySelector(".resp-text");
    if (rt) rt.textContent = st.content;
    let think = el.querySelector(".think");
    if (st.reasoning && !think) {
      // Reasoning started streaming after the card was built without one: add an
      // expanded thinking block (it auto-collapses when the exchange completes).
      think = document.createElement("details");
      think.className = "think";
      think.open = true;
      const summary = document.createElement("summary");
      summary.className = "think-summary";
      const text = document.createElement("pre");
      text.className = "think-text";
      think.append(summary, text);
      wireThink(think);
      if (rt) rt.before(think);
    }
    if (think) {
      const tt = think.querySelector(".think-text");
      tt.textContent = st.reasoning;
      think.querySelector(".think-summary").textContent = thinkSummary(st.reasoning);
      if (think._stick) tt.scrollTop = tt.scrollHeight;
    }
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
      case "ping":
        // Liveness reply; the server prunes sockets that go silent (hub._ping_loop).
        if (state.ws) state.ws.send(JSON.stringify({ type: "pong", ts: ev.ts }));
        break;
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
            sequence: ev.sequence,
            is_replay: !!ev.is_replay,
            client_request: ev.client_request,
            server_response: { status: "…", streaming: ev.streaming },
            timings: {},
            usage: null,
            in_flight: true,
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

  function wslog(level, ...args) {
    console[level](`[ws] ${new Date().toISOString().slice(11, 23)}`, ...args);
  }

  function connectWs() {
    if (state.wsRetry) {
      clearTimeout(state.wsRetry);
      state.wsRetry = null;
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws`;
    const attempt = ++state.wsAttempts;
    state.wsOpenedAt = null; // a close before open must not report the old socket's age
    wslog("log", `connect #${attempt} -> ${url}`);
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => {
      setWs(true);
      state.wsOpenedAt = Date.now();
      wslog("log", `open (attempt #${attempt})${state.focusCid ? `, resubscribing ${state.focusCid}` : ""}`);
      if (state.focusCid) ws.send(JSON.stringify({ type: "subscribe", conversation_id: state.focusCid }));
      // Any (re)connect means we may have missed broadcast events (client_seen)
      // while the socket was down; re-sync the dock. Cheap: one small GET.
      refreshClients();
    };
    ws.onmessage = (m) => {
      state.wsLastMsgAt = Date.now();
      try {
        const ev = JSON.parse(m.data);
        wslog("debug", `event: ${ev.type}`);
        onWsMessage(ev);
      } catch (e) {
        wslog("error", "message handling failed", e, m.data);
      }
    };
    ws.onclose = (ev) => {
      if (state.ws !== ws) return; // superseded by a newer connection
      setWs(false);
      const lived = state.wsOpenedAt ? ` lived=${Math.round((Date.now() - state.wsOpenedAt) / 1000)}s` : "";
      if (ev.code === 1000) wslog("log", `closed code=1000 (clean)${lived}`);
      else wslog("error", `closed code=${ev.code} reason=${esc(ev.reason || "-")}${lived}`);
      state.wsRetry = setTimeout(connectWs, 2000);
      wslog("log", "reconnect in 2s");
    };
    ws.onerror = (ev) => {
      wslog("error", "socket error", ev);
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    };
  }

  function checkWsStale() {
    // A socket can be open but half-dead (observed in Firefox: pill says
    // ws:on, nothing flows). Only warn once we have seen traffic, so an idle
    // proxy (no proxied requests) stays quiet.
    if (state.ws && state.ws.readyState === 1 && state.wsLastMsgAt) {
      const silent = Math.round((Date.now() - state.wsLastMsgAt) / 1000);
      if (silent > 60) wslog("warn", `open but no message for ${silent}s`);
    }
  }

  // ---------- misc ----------
  async function checkUpstream() {
    let ok = false;
    let label = "upstream: error";
    try {
      const h = await api("/health");
      ok = h.upstream === "ok";
      label = `upstream: ${h.upstream}`;
    } catch {
      /* proxy unreachable */
    }
    $("stat-upstream").textContent = label;
    $("stat-upstream").classList.toggle("ok", ok);
    $("stat-upstream").classList.toggle("ws-off", !ok);
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
      closeReplayDock();
    });
    $("stick").addEventListener("change", (e) => {
      state.stick = e.target.checked;
      if (state.stick) maybeScroll();
    });
    $("filter").addEventListener("input", (e) => renderDock(e.target.value));
  }

  async function init() {
    wireButtons();
    wireReplayDock();
    checkUpstream();
    setInterval(() => {
      checkUpstream(); // keep the upstream pill honest (recovers when it comes back)
      checkWsStale();
    }, 30000);
    await refreshClients();
    connectWs();
  }
  init();
})();

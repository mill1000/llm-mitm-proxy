"""UI REST API. Serves conversation data, replay, and export to the web UI.

Endpoints: list clients, remove a client, read a conversation, read or remove
one exchange, replay an exchange (re-send its captured client request
upstream), export a conversation or a single exchange, clear a conversation,
and best-effort model metadata (context windows) from the upstream.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from ..dump import conversation_to_dump, exchange_to_dump

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.get("/clients")
async def list_clients(request: Request):
    store = _store(request)
    out = []
    for c in store.list_clients():
        convs = [cv for cv in store.all_conversations() if cv.client_id == c.id]
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "first_seen": c.first_seen,
                "last_seen": c.last_seen,
                "conversation_ids": [cv.id for cv in convs],
            }
        )
    out.sort(key=lambda x: x["last_seen"], reverse=True)
    return out


@router.delete("/clients/{client_id}")
async def remove_client(client_id: str, request: Request):
    """Drop a client and all of its conversations; it re-registers on the next request."""
    store = _store(request)
    if not store.remove_client(client_id):
        raise HTTPException(status_code=404, detail="client not found")
    return {"ok": True}


@router.get("/models")
async def list_models(request: Request):
    """Best-effort model metadata from the upstream's ``/v1/models``.

    The proxy is protocol-agnostic, so any failure (upstream down, non-OpenAI
    body, no context info) degrades to an empty list rather than an error; the
    UI then simply hides the context-usage widget it cannot size.
    """
    try:
        r = await request.app.state.http.get("/v1/models", timeout=5.0)
        r.raise_for_status()
        data = r.json()
    except Exception:  # noqa: BLE001
        return {"models": []}
    entries = data.get("data") if isinstance(data, dict) else None
    models = []
    for m in entries or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        models.append({"id": m["id"], "context": _model_context(m)})
    return {"models": models}


def _model_context(m: dict) -> int | None:
    """The model's context window in tokens, if the upstream reports one.

    Prefers the *running* context, which is what a client can actually fill:
    top-level ``n_ctx`` (plain llama.cpp) or ``meta.n_ctx`` (multi-model
    launchers). Falls back to the trained window (``n_ctx_train``), a generic
    ``context_length``, and finally ``max_tokens``.
    """
    meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
    for source, key in (
        (m, "n_ctx"),
        (meta, "n_ctx"),
        (m, "n_ctx_train"),
        (meta, "n_ctx_train"),
        (m, "context_length"),
        (m, "max_tokens"),
    ):
        v = source.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


@router.get("/conversations/{cid}")
async def get_conversation(cid: str, request: Request, limit: int = Query(200, ge=1, le=5000)):
    store = _store(request)
    conv = store.get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    exchanges = conv.exchanges[-limit:]
    return {
        "id": conv.id,
        "client_id": conv.client_id,
        "tag": conv.tag,
        "created_at": conv.created_at,
        "exchange_count": len(conv.exchanges),
        "exchanges": [ex.to_dict() for ex in exchanges],
    }


@router.get("/conversations/{cid}/exchanges/{seq}")
async def get_exchange(cid: str, seq: int, request: Request):
    store = _store(request)
    conv = store.get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    for ex in conv.exchanges:
        if ex.sequence == seq:
            return ex.to_dict()
    raise HTTPException(status_code=404, detail="exchange not found")


@router.delete("/conversations/{cid}/exchanges/{seq}")
async def remove_exchange(cid: str, seq: int, request: Request):
    """Remove an exchange from the conversation.

    In-flight exchanges may be removed too: the upstream call continues but its
    finalization is dropped, so no completed event is emitted for it.
    """
    store = _store(request)
    if not store.remove_exchange(cid, seq):
        raise HTTPException(status_code=404, detail="conversation or exchange not found")
    return {"ok": True}


@router.post("/conversations/{cid}/exchanges/{seq}/replay")
async def replay_exchange(cid: str, seq: int, request: Request):
    """Re-send the captured client request of exchange ``seq`` upstream.

    The JSON body, if given and non-empty, is the full request body to send in
    place of the captured one; otherwise the captured request is re-sent as-is.
    The replay is captured into the same conversation and flagged ``is_replay``.
    """
    store = _store(request)
    conv = store.get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    source = next((ex for ex in conv.exchanges if ex.sequence == seq), None)
    if source is None:
        raise HTTPException(status_code=404, detail="exchange not found")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        body = None
    return await request.app.state.pipeline.replay(conv, source, body)


@router.get("/conversations/{cid}/export")
async def export_conversation(cid: str, request: Request, format: str = Query("json")):
    if format != "json":
        raise HTTPException(status_code=400, detail=f"unsupported format: {format}")
    store = _store(request)
    settings = request.app.state.settings
    conv = store.get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    payload = conversation_to_dump(conv, settings)
    body = json.dumps(payload, indent=2).encode()
    return Response(
        content=body,
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{cid}.json"'},
    )


@router.get("/conversations/{cid}/exchanges/{seq}/export")
async def export_exchange(cid: str, seq: int, request: Request, format: str = Query("json")):
    if format != "json":
        raise HTTPException(status_code=400, detail=f"unsupported format: {format}")
    store = _store(request)
    conv = store.get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    ex = next((e for e in conv.exchanges if e.sequence == seq), None)
    if ex is None:
        raise HTTPException(status_code=404, detail="exchange not found")
    payload = exchange_to_dump(ex, request.app.state.settings)
    body = json.dumps(payload, indent=2).encode()
    return Response(
        content=body,
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{cid}-ex{seq}.json"'},
    )


@router.delete("/conversations/{cid}")
async def clear_conversation(cid: str, request: Request):
    store = _store(request)
    if not store.clear_conversation(cid):
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"ok": True}

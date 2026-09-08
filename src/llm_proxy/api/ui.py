"""UI REST API. Serves conversation data, replay (M2), and export to the web UI.

M0 provides: list clients, read a conversation, read one exchange, export, clear.
Replay lands in M2 (it needs the captured client request re-sent to the upstream).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from ..dump import conversation_to_dump

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


@router.delete("/conversations/{cid}")
async def clear_conversation(cid: str, request: Request):
    store = _store(request)
    if not store.clear_conversation(cid):
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"ok": True}

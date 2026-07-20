"""GLC's inbound A2A surface: Agent Card discovery plus JSON-RPC task methods."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from s13code.core.a2a_adapter.push_receiver import PushAuthError

router = APIRouter(tags=["A2A"])


@router.get("/.well-known/agent-card.json")
async def agent_card(request: Request):
    return await request.app.state.s13_a2a.agent_card()


@router.post("/a2a")
async def a2a_rpc(request: Request):
    return await request.app.state.s13_a2a.rpc(request)


@router.post("/v1/a2a/push")
async def a2a_push(request: Request):
    """Durable inbound push: verifies, deduplicates, and resumes a waiting node.

    See ``core/a2a_adapter/push_receiver.py``. A malformed or unauthenticated
    delivery is rejected with 401 rather than silently mutating the graph.
    """
    try:
        return await request.app.state.s13_push_receiver.handle(dict(request.headers), await request.body())
    except PushAuthError as error:
        raise HTTPException(401, str(error)) from error

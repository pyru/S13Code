"""Durable inbound A2A push: a waiting node resumes from a real signed webhook.

This exercises the receiver half that Session 13 section 13/14 describes but
``official.py`` never implemented: ``A2ADemoServer._notify`` already signs
and idempotency-keys outbound pushes (see ``test_hardening.py``); these tests
prove something actually receives, verifies, deduplicates, and durably
resumes the graph from them -- including across a simulated process restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi import FastAPI, Request

from s13code.core.a2a_adapter.client import A2AClient
from s13code.core.a2a_adapter.push_receiver import DurablePushReceiver, PushAuthError, PushCorrelationLedger
from s13code.core.a2a_adapter.schema import AgentCardError
from s13code.core.a2a_adapter.server import A2ADemoServer
from s13code.core.a2a_adapter.trust import AgentCardTrustPolicy
from s13code.core.live_graph import GraphPatch, GraphStore, LiveGraphExecutor, TaskSpec

PUSH_SECRET = "wh-shared-secret"
PUSH_TOKEN = "receiver-bearer-token"


def remote_card():
    return {
        "name": "remote specialist", "description": "async push fixture", "version": "1.0.0",
        "supportedInterfaces": [{"url": "http://remote.test/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": True, "pushNotifications": True},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "remote-report", "name": "Remote report", "description": "Async work", "tags": ["test"]}],
    }


class WaitingGraphPlanner:
    """Minimal planner: park one node in waiting, finish once it resumes and succeeds."""

    async def plan(self, graph, event):
        if event.kind == "run_started":
            node = TaskSpec("remote", "remote_result", {}, {"agent": "remote_specialist"})
            return GraphPatch(add=(node,), wait=("remote",), reason="parked pending durable push")
        if event.node_id == "remote" and event.kind == "task_succeeded":
            return GraphPatch(finish=True, reason="remote artifact joined graph")
        return GraphPatch()


def receiver_app(receiver: DurablePushReceiver) -> FastAPI:
    app = FastAPI()

    @app.post("/push")
    async def push(request: Request):
        return await receiver.handle(dict(request.headers), await request.body())

    return app


@pytest.fixture
def graph(tmp_path):
    store = GraphStore(tmp_path / "graph.sqlite")
    yield store
    store.close()


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.sqlite"
    made = PushCorrelationLedger(path)
    yield made, path
    made.close()


def remote_result_skill(ledger: PushCorrelationLedger, run_id: str):
    async def skill(task: TaskSpec) -> dict:
        artifact = ledger.artifact(run_id, task.id)
        if artifact is None:
            raise RuntimeError("resumed before an artifact was delivered")
        return {"text": artifact.text, "remote_task_id": artifact.task_id, "state": artifact.state}
    return skill


@pytest.mark.asyncio
async def test_signed_push_durably_resumes_a_waiting_node_and_continues_the_graph(graph, ledger):
    ledger_store, _ = ledger
    run_id = "remote-run"
    continued: list[str] = []

    async def continuation(resumed_run_id: str) -> None:
        continued.append(resumed_run_id)
        executor = LiveGraphExecutor(graph, WaitingGraphPlanner(), {"remote_result": remote_result_skill(ledger_store, run_id)})
        await executor.run(resumed_run_id, resume=True)

    receiver = DurablePushReceiver(ledger=ledger_store, store=graph, signing_secret=PUSH_SECRET,
                                   bearer_token=PUSH_TOKEN, continuation=continuation)
    app = receiver_app(receiver)
    receiver_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://receiver.test")
    remote_server = A2ADemoServer(remote_card(), push_http=receiver_http, push_signing_secret=PUSH_SECRET)
    remote_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=remote_server.app), base_url="http://remote.test")
    trust = AgentCardTrustPolicy(key_resolver=lambda kid: None, require_signature=False)
    client = A2AClient(remote_http, trust)

    executor = LiveGraphExecutor(graph, WaitingGraphPlanner(), {"remote_result": remote_result_skill(ledger_store, run_id)})
    parked = await executor.run(run_id)
    assert parked.waiting == ("remote",)

    agent = await client.discover("http://remote.test")
    started = await client.send(agent, "slow remote task", push_url="http://receiver.test/push", push_token=PUSH_TOKEN)
    assert started["status"]["state"] == "working"
    ledger_store.register(task_id=started["id"], run_id=run_id, node_id="remote")

    for _ in range(50):
        if graph.node_state(run_id, "remote") == "succeeded":
            break
        await asyncio.sleep(0.01)

    assert continued == [run_id]
    assert graph.snapshot(run_id).finished
    result = graph.snapshot(run_id).nodes["remote"]["result"]
    assert result["remote_task_id"] == started["id"]
    assert result["state"] == "completed"

    await receiver_http.aclose()
    await remote_http.aclose()
    await remote_server.close()


@pytest.mark.asyncio
async def test_before_the_fix_check_then_act_on_state_alone_is_racy(graph):
    """The failure ``PushCorrelationLedger.claim`` exists to prevent.

    A tempting simpler receiver skips the idempotency ledger and only asks
    "is the node still waiting?" before resuming it. That is safe for two
    deliveries handled strictly one after another, but at-least-once webhook
    transports do not promise that: a redelivery can reach a *second* worker
    process while the first is still mid-flight. Both workers pass the same
    "still waiting?" check before either has committed a resume -- a classic
    check-then-act race -- and the loser's write is rejected outright.

    ``claim()`` fixes this with a single atomic ``INSERT OR IGNORE`` (see
    ``push_receiver.py``): only one caller can ever win it for a given
    ``Idempotency-Key``, so a second worker never reaches the graph at all
    (``test_a_replayed_webhook_is_suppressed_not_reapplied`` below proves the
    fixed receiver returns a clean ``"duplicate"`` instead of this crash).
    """
    from s13code.core.live_graph.store import GraphMutationError

    run_id = "race-run"
    executor = LiveGraphExecutor(graph, WaitingGraphPlanner(), {"remote_result": lambda t: {}})
    await executor.run(run_id)

    # Two workers both observe WAITING before either has resumed the node --
    # the interleaving an atomic claim exists to rule out.
    assert graph.node_state(run_id, "remote") == "waiting"
    assert graph.node_state(run_id, "remote") == "waiting"

    worker_a_event = graph.record_external_event(run_id, "a2a_push_received", "remote", {})
    graph.apply_patch(run_id, GraphPatch(resume=("remote",), reason="worker A"), trigger_event=worker_a_event.sequence)

    worker_b_event = graph.record_external_event(run_id, "a2a_push_received", "remote", {})
    with pytest.raises(GraphMutationError, match="can only resume waiting task"):
        graph.apply_patch(run_id, GraphPatch(resume=("remote",), reason="worker B"), trigger_event=worker_b_event.sequence)


@pytest.mark.asyncio
async def test_a_replayed_webhook_is_suppressed_not_reapplied(graph, ledger):
    """Adversarial: at-least-once delivery redelivers the identical push."""
    ledger_store, _ = ledger
    run_id = "replay-run"
    applications: list[str] = []

    async def continuation(resumed_run_id: str) -> None:
        applications.append(resumed_run_id)

    receiver = DurablePushReceiver(ledger=ledger_store, store=graph, signing_secret=PUSH_SECRET,
                                   bearer_token=None, continuation=continuation)

    executor = LiveGraphExecutor(graph, WaitingGraphPlanner(), {"remote_result": remote_result_skill(ledger_store, run_id)})
    await executor.run(run_id)
    ledger_store.register(task_id="remote-task-1", run_id=run_id, node_id="remote")

    headers, body = _signed_push("remote-task-1", "completed", "the remote answer", idempotency_key="evt-1")

    first = await receiver.handle(headers, body)
    assert first["status"] == "resumed"
    assert applications == [run_id]
    assert graph.node_state(run_id, "remote") == "pending"

    # Same idempotency key, same payload: the transport redelivered it.
    second = await receiver.handle(headers, body)
    assert second["status"] == "duplicate"
    assert applications == [run_id]  # not called again
    assert graph.node_state(run_id, "remote") == "pending"  # not double-resumed


@pytest.mark.asyncio
async def test_duplicate_suppression_survives_process_restart(tmp_path, graph):
    run_id = "restart-run"
    executor = LiveGraphExecutor(graph, WaitingGraphPlanner(), {"remote_result": lambda t: {}})
    await executor.run(run_id)

    ledger_path = tmp_path / "restart-ledger.sqlite"
    first_ledger = PushCorrelationLedger(ledger_path)
    first_ledger.register(task_id="remote-task-r", run_id=run_id, node_id="remote")
    headers, body = _signed_push("remote-task-r", "completed", "answer", idempotency_key="evt-restart")

    async def continuation(_run_id: str) -> None:
        return None

    receiver_one = DurablePushReceiver(ledger=first_ledger, store=graph, signing_secret=PUSH_SECRET,
                                       bearer_token=None, continuation=continuation)
    first = await receiver_one.handle(headers, body)
    assert first["status"] == "resumed"
    first_ledger.close()

    # A fresh process re-opens the same ledger file.
    second_ledger = PushCorrelationLedger(ledger_path)
    receiver_two = DurablePushReceiver(ledger=second_ledger, store=graph, signing_secret=PUSH_SECRET,
                                       bearer_token=None, continuation=continuation)
    replay = await receiver_two.handle(headers, body)
    assert replay["status"] == "duplicate"
    second_ledger.close()


@pytest.mark.asyncio
async def test_tampered_signature_is_rejected(graph, ledger):
    ledger_store, _ = ledger
    receiver = DurablePushReceiver(ledger=ledger_store, store=graph, signing_secret=PUSH_SECRET,
                                   bearer_token=None, continuation=lambda run_id: asyncio.sleep(0))
    headers, body = _signed_push("t1", "completed", "x", idempotency_key="evt-bad", secret="wrong-secret")
    with pytest.raises(PushAuthError):
        await receiver.handle(headers, body)


@pytest.mark.asyncio
async def test_missing_bearer_token_is_rejected(graph, ledger):
    ledger_store, _ = ledger
    receiver = DurablePushReceiver(ledger=ledger_store, store=graph, signing_secret=None,
                                   bearer_token=PUSH_TOKEN, continuation=lambda run_id: asyncio.sleep(0))
    headers, body = _signed_push("t1", "completed", "x", idempotency_key="evt-noauth", secret=None)
    with pytest.raises(PushAuthError):
        await receiver.handle(headers, body)


@pytest.mark.asyncio
async def test_push_arriving_after_local_cancellation_does_not_resurrect_the_node(graph, ledger):
    ledger_store, _ = ledger
    run_id = "cancel-run"
    executor = LiveGraphExecutor(graph, WaitingGraphPlanner(), {"remote_result": lambda t: {}})
    await executor.run(run_id)
    ledger_store.register(task_id="remote-task-c", run_id=run_id, node_id="remote")

    cancel_event = graph.record_external_event(run_id, "graph_cancel_requested", "remote", {})
    graph.apply_patch(run_id, GraphPatch(cancel=("remote",), finish=True, reason="caller cancelled"),
                      trigger_event=cancel_event.sequence)
    assert graph.node_state(run_id, "remote") == "cancelled"

    continuations: list[str] = []

    async def continuation(resumed_run_id: str) -> None:
        continuations.append(resumed_run_id)

    receiver = DurablePushReceiver(ledger=ledger_store, store=graph, signing_secret=PUSH_SECRET,
                                   bearer_token=None, continuation=continuation)
    headers, body = _signed_push("remote-task-c", "completed", "late result", idempotency_key="evt-late")
    result = await receiver.handle(headers, body)

    assert result["status"] == "ignored_not_waiting"
    assert continuations == []
    assert graph.node_state(run_id, "remote") == "cancelled"


@pytest.mark.asyncio
async def test_discovery_negotiates_a_mutually_supported_binding_and_version():
    """Discovery picks JSONRPC/1.0 out of a multi-interface card, and refuses a card with no overlap."""
    multi = remote_card()
    multi["supportedInterfaces"] = [
        {"url": "dns:///remote.test:443", "protocolBinding": "GRPC", "protocolVersion": "1.0"},
        {"url": "http://remote.test/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ]
    server = A2ADemoServer(multi)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://remote.test")
    trust = AgentCardTrustPolicy(key_resolver=lambda kid: None, require_signature=False)
    try:
        agent = await A2AClient(http, trust).discover("http://remote.test")
        # The gRPC entry is listed first (server preference) but this client
        # speaks JSONRPC, so negotiation must skip it rather than take index 0.
        assert agent.endpoint == "http://remote.test/a2a"
        assert agent.card["name"] == "remote specialist"

        future_only = remote_card()
        future_only["supportedInterfaces"] = [
            {"url": "http://remote.test/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "9.9"}]
        future_server = A2ADemoServer(future_only)
        future_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=future_server.app),
                                        base_url="http://remote.test")
        with pytest.raises(AgentCardError, match="No mutually supported"):
            await A2AClient(future_http, trust).discover("http://remote.test")
        await future_http.aclose()
        await future_server.close()
    finally:
        await http.aclose()
        await server.close()


@pytest.mark.asyncio
async def test_remote_cancellation_propagates_and_its_push_carries_the_canceled_state(graph, ledger):
    """Cancelling the remote A2A task is durable and reaches the graph as a terminal outcome."""
    ledger_store, _ = ledger
    run_id = "remote-cancel-run"
    receiver = DurablePushReceiver(ledger=ledger_store, store=graph, signing_secret=PUSH_SECRET,
                                   bearer_token=None, continuation=lambda rid: asyncio.sleep(0))
    app = receiver_app(receiver)
    receiver_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://receiver.test")
    server = A2ADemoServer(remote_card(), push_http=receiver_http, push_signing_secret=PUSH_SECRET)
    remote_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://remote.test")
    trust = AgentCardTrustPolicy(key_resolver=lambda kid: None, require_signature=False)
    client = A2AClient(remote_http, trust)

    executor = LiveGraphExecutor(graph, WaitingGraphPlanner(), {"remote_result": remote_result_skill(ledger_store, run_id)})
    await executor.run(run_id)

    try:
        agent = await client.discover("http://remote.test")
        started = await client.send(agent, "slow cancellable work", push_url="http://receiver.test/push")
        assert started["status"]["state"] == "working"   # task progress, pre-terminal
        ledger_store.register(task_id=started["id"], run_id=run_id, node_id="remote")

        cancelled = await client.cancel(agent, started["id"])
        assert cancelled["status"]["state"] == "canceled"
        # tasks/get confirms the remote server persisted the cancellation.
        assert (await client.get(agent, started["id"]))["status"]["state"] == "canceled"

        # The cancellation push was delivered and journalled as a terminal
        # outcome, so the graph learns the work stopped instead of hanging.
        for _ in range(50):
            if graph.node_state(run_id, "remote") != "waiting":
                break
            await asyncio.sleep(0.01)
        assert graph.node_state(run_id, "remote") == "pending"
        assert ledger_store.artifact(run_id, "remote").state == "canceled"
    finally:
        await receiver_http.aclose()
        await remote_http.aclose()
        await server.close()


@pytest.mark.asyncio
async def test_a_push_resumes_a_waiting_node_after_a_full_process_restart(tmp_path):
    """Both the graph journal and the correlation ledger are reopened from disk.

    This is the case an in-memory subscription (``official.py``'s gRPC
    ``SubscribeToTask``) structurally cannot serve: nothing is held open
    across the restart, yet the node still resumes exactly once.
    """
    graph_path, ledger_path = tmp_path / "restart-graph.sqlite", tmp_path / "restart-ledger.sqlite"
    run_id = "full-restart-run"

    first_graph, first_ledger = GraphStore(graph_path), PushCorrelationLedger(ledger_path)
    executor = LiveGraphExecutor(first_graph, WaitingGraphPlanner(), {"remote_result": remote_result_skill(first_ledger, run_id)})
    parked = await executor.run(run_id)
    assert parked.waiting == ("remote",)
    first_ledger.register(task_id="task-across-restart", run_id=run_id, node_id="remote")
    # The whole process dies here: no open sockets, no live subscription.
    first_graph.close()
    first_ledger.close()

    second_graph, second_ledger = GraphStore(graph_path), PushCorrelationLedger(ledger_path)
    try:
        assert second_graph.node_state(run_id, "remote") == "waiting"   # durable across the restart
        resumed: list[str] = []

        async def continuation(rid: str) -> None:
            resumed.append(rid)
            await LiveGraphExecutor(second_graph, WaitingGraphPlanner(),
                                    {"remote_result": remote_result_skill(second_ledger, run_id)}).run(rid, resume=True)

        receiver = DurablePushReceiver(ledger=second_ledger, store=second_graph, signing_secret=PUSH_SECRET,
                                       bearer_token=None, continuation=continuation)
        headers, body = _signed_push("task-across-restart", "completed", "artifact that outlived the process",
                                     idempotency_key="evt-across-restart")
        assert (await receiver.handle(headers, body))["status"] == "resumed"

        assert resumed == [run_id]
        snapshot = second_graph.snapshot(run_id)
        assert snapshot.finished
        assert snapshot.nodes["remote"]["result"]["text"] == "artifact that outlived the process"
        # And the replay of that same delivery is still suppressed post-restart.
        assert (await receiver.handle(headers, body))["status"] == "duplicate"
    finally:
        second_graph.close()
        second_ledger.close()


def _signed_push(task_id: str, state: str, text: str, *, idempotency_key: str,
                 secret: str | None = PUSH_SECRET) -> tuple[dict[str, str], bytes]:
    payload = {
        "jsonrpc": "2.0", "method": "tasks/statusUpdate",
        "params": {"kind": "task", "id": task_id, "contextId": "ctx",
                   "status": {"state": state, "timestamp": "now"},
                   "artifacts": [{"artifactId": f"result-{task_id}", "parts": [{"kind": "text", "text": text}]}]},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key, "A2A-Task-ID": task_id}
    if secret:
        headers["A2A-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return headers, body

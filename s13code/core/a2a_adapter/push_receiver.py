"""Durable inbound A2A push receiver: the caller-side half of asynchronous push.

``A2ADemoServer._notify`` already sends a signed, idempotency-keyed webhook
when a remote task changes state (see ``server.py``). Nothing in this
codebase previously *received* that webhook and turned it back into a live
graph event: the only existing waiting/resume bridge is the gRPC
``SubscribeToTask`` path in ``official.py``, which keeps a connection open
and cannot survive this process restarting mid-wait.

This module is the missing half of the asynchronous-push interaction mode
from Session 13 section 13: a caller dispatches a task, parks the graph node
in ``waiting``, releases every local resource, and later a webhook arrives
-- possibly after a restart, possibly more than once -- and must resume
*exactly once* the node it names.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from s13code.core.live_graph import GraphPatch, GraphStore, NodeState

Continuation = Callable[[str], Awaitable[None]]


class PushAuthError(ValueError):
    """The inbound push failed authentication or signature verification."""


@dataclass(frozen=True)
class PushArtifact:
    task_id: str
    text: str
    state: str


class PushCorrelationLedger:
    """Durable remote-task correlation, delivered artifacts, and delivery receipts.

    Everything here is SQLite so the mapping from a remote ``task_id`` back
    to the local ``(run_id, node_id)`` -- and the record of which webhook
    deliveries were already applied -- survives a process crash between
    dispatch and push arrival.
    """

    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS a2a_dispatches(
            task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
            artifact_text TEXT, state TEXT, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS a2a_push_receipts(
            idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            applied INTEGER NOT NULL DEFAULT 0, received_at TEXT NOT NULL
        );
        """)
        self.db.commit()

    def register(self, *, task_id: str, run_id: str, node_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO a2a_dispatches(task_id, run_id, node_id) VALUES (?,?,?)",
                (task_id, run_id, node_id),
            )

    def lookup(self, task_id: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT run_id, node_id FROM a2a_dispatches WHERE task_id=?", (task_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def store_artifact(self, task_id: str, *, text: str, state: str) -> None:
        with self.db:
            cursor = self.db.execute(
                "UPDATE a2a_dispatches SET artifact_text=?, state=?, delivered=1 WHERE task_id=?",
                (text, state, task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"cannot store artifact for undispatched task {task_id!r}")

    def artifact(self, run_id: str, node_id: str) -> PushArtifact | None:
        row = self.db.execute(
            "SELECT task_id, artifact_text, state FROM a2a_dispatches "
            "WHERE run_id=? AND node_id=? AND delivered=1", (run_id, node_id),
        ).fetchone()
        return PushArtifact(row[0], row[1] or "", row[2]) if row else None

    def claim(self, idempotency_key: str, task_id: str) -> bool:
        """Atomically claim a delivery. True only for the first claimant.

        A redelivered webhook (same ``Idempotency-Key``, at-least-once
        transport) loses this race on every subsequent attempt and is
        reported back to the caller as a no-op duplicate instead of being
        reapplied to the graph.
        """
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO a2a_push_receipts(idempotency_key, task_id, applied, received_at) "
                "VALUES (?,?,0,?)",
                (idempotency_key, task_id, datetime.now(UTC).isoformat()),
            )
            return cursor.rowcount == 1

    def mark_applied(self, idempotency_key: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE a2a_push_receipts SET applied=1 WHERE idempotency_key=?", (idempotency_key,)
            )

    def close(self) -> None:
        self.db.close()


class DurablePushReceiver:
    """Verifies, deduplicates, and applies one inbound A2A push notification.

    ``continuation`` is called after a node is durably resumed so the live
    graph actually keeps running (in S13Code this is ``S13Runtime.run(...,
    resume=True)``); a failure inside it does not un-resume the node or
    replay the webhook -- the resume is already journalled, and the run's
    own event trace is where that downstream failure belongs.
    """

    def __init__(self, *, ledger: PushCorrelationLedger, store: GraphStore,
                 signing_secret: str | None, bearer_token: str | None,
                 continuation: Continuation) -> None:
        self.ledger, self.store = ledger, store
        self.signing_secret, self.bearer_token = signing_secret, bearer_token
        self.continuation = continuation

    async def handle(self, headers: Mapping[str, str], raw_body: bytes) -> dict[str, Any]:
        headers = {key.lower(): value for key, value in headers.items()}
        self._authenticate(headers, raw_body)

        idempotency_key = headers.get("idempotency-key")
        task_id = headers.get("a2a-task-id")
        if not idempotency_key or not task_id:
            raise PushAuthError("push is missing Idempotency-Key or A2A-Task-ID header")

        if not self.ledger.claim(idempotency_key, task_id):
            return {"status": "duplicate", "task_id": task_id}

        correlation = self.ledger.lookup(task_id)
        if correlation is None:
            self.ledger.mark_applied(idempotency_key)
            return {"status": "unknown_task", "task_id": task_id}
        run_id, node_id = correlation

        body = json.loads(raw_body)
        task_wire = body.get("params", body)
        state = task_wire.get("status", {}).get("state")
        if state not in {"completed", "failed", "canceled"}:
            self.ledger.mark_applied(idempotency_key)
            return {"status": "ack", "task_id": task_id}

        text = "".join(
            part.get("text", "")
            for artifact in task_wire.get("artifacts", [])
            for part in artifact.get("parts", [])
        )
        self.ledger.store_artifact(task_id, text=text, state=state)

        if self.store.node_state(run_id, node_id) != NodeState.WAITING:
            # Cancelled locally, already resumed by another transport (e.g. a
            # concurrent gRPC subscription), or resumed by an earlier -- since
            # discarded -- delivery of this very webhook.  The artifact is
            # kept for audit; the graph is not touched.
            self.ledger.mark_applied(idempotency_key)
            return {"status": "ignored_not_waiting", "task_id": task_id}

        event = self.store.record_external_event(
            run_id, "a2a_push_received", node_id, {"remote_task_id": task_id, "state": state},
        )
        self.store.apply_patch(
            run_id, GraphPatch(resume=(node_id,), reason="durable A2A push resumed waiting node"),
            trigger_event=event.sequence,
        )
        self.ledger.mark_applied(idempotency_key)

        try:
            await self.continuation(run_id)
        except Exception as error:  # the resume already happened; report, don't roll back
            return {"status": "resumed_continuation_failed", "task_id": task_id,
                    "error": f"{type(error).__name__}: {error}"}
        return {"status": "resumed", "task_id": task_id}

    def _authenticate(self, headers: dict[str, str], raw_body: bytes) -> None:
        if self.bearer_token:
            authorization = headers.get("authorization", "")
            token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
            if not (token and hmac.compare_digest(token, self.bearer_token)):
                raise PushAuthError("missing or invalid bearer token")
        if self.signing_secret:
            expected = "sha256=" + hmac.new(self.signing_secret.encode(), raw_body, hashlib.sha256).hexdigest()
            got = headers.get("a2a-signature", "")
            if not (got and hmac.compare_digest(got, expected)):
                raise PushAuthError("invalid or missing A2A-Signature")

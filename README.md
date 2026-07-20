# S13Code

`S13Code` is the standalone Session 13 agent runtime. It implements a live task graph, scoped and provenance-bearing memory, Rohan's semantic chunking V2, and Agent2Agent interoperability. It asks `glc_v3` for model completions over HTTP and never owns provider credentials.

## What runs where

| Service | Default address | Responsibility |
|---|---|---|
| `glc_v3` | `http://127.0.0.1:8111` | Models, keys, routing and channels |
| `S13Code` HTTP | `http://127.0.0.1:8113` | Graph, memory, documents and JSON-RPC A2A |
| `S13Code` gRPC | `127.0.0.1:8114` | Official A2A gRPC service |
| Ollama | `http://127.0.0.1:11434` | Phi-4 segmentation and Nomic embeddings |

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A running `glc_v3`
- A running Ollama with `phi4` and `nomic-embed-text`

```bash
ollama pull phi4
ollama pull nomic-embed-text
ollama serve
```

## Install and run

Unzip `glc_v3`, `S13Code`, and `S13Proof` beside one another. Start `glc_v3` first. Then, from this directory:

```bash
uv sync

export GLC_BASE_URL=http://127.0.0.1:8111
export S13_GATEWAY_PROVIDER=gemini
export S13_SANDBOX_ROOT="$PWD/sandbox"
export S13_CHUNK_MODEL=phi4:latest
export S13_LIVE_SEMANTIC_CHUNKING=1

uv run s13code serve
```

State is written under `~/.s13code` by default. Set `S13_DATA_DIR` to use another directory.

Check both services:

```bash
curl http://127.0.0.1:8111/healthz
curl http://127.0.0.1:8113/healthz
curl http://127.0.0.1:8113/readyz
curl http://127.0.0.1:8113/.well-known/agent-card.json
```

## Run a prompt

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "s13",
    "user_id": "student-01",
    "agent_id": "assistant",
    "prompt": "Say hello."
  }'
```

The response contains the final answer, graph nodes and edges, ordered graph events, and provider/agent assignments. Inspect a persisted run with:

```bash
curl http://127.0.0.1:8113/v1/agent/runs/<run-id>
```

## Index the sample corpus

The five files under `sandbox/papers/` are fixed `.txt` fixtures for semantic chunking and retrieval proofs.

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "papers",
    "user_id": "student-01",
    "prompt": "Index every .txt file under papers/. Confirm how many chunks were indexed in total."
  }'
```

Document ingestion is versioned and atomic: source preparation, semantic boundaries, exact spans, Nomic embeddings, and visibility succeed together or roll back together.

## Architecture

- `s13code/core/live_graph/`: durable graph state, patches, event replay and bounded parallel execution
- `s13code/core/memory/`: scope checks, provenance, contradiction history, semantic chunking and FAISS retrieval
- `s13code/core/a2a_adapter/`: Agent Cards, JSON-RPC, SSE/push, official gRPC and trust checks
- `s13code/gateway.py`: the only `S13Code → glc_v3` seam
- `s13code/runtime.py`: joins graph, memory, tools and model calls into an inspectable run
- `tests/`: executable invariants and regression cases

## Test before opening a pull request

```bash
uv run ruff check .
uv run pytest -q

cd ../S13Proof
uv sync
uv run pytest -q
```

## Student contribution

Fork the official `S13Code` repository linked from Axiom, create a branch, implement one meaningful extension, and open one pull request against that repository. Do not open the Session 13 pull request against `glc_v3`.

Add one subsection to this README in the same pull request. It must contain:

1. the user-visible capability,
2. the exact prompt or API request,
3. the graph and ordered event trace,
4. the actual final result,
5. evidence and provider/agent assignments,
6. the adversarial failure and its fix,
7. commands that reproduce the result from a fresh checkout.

Do not commit `.env`, credentials, personal memory, generated databases, unrestricted local paths, benchmark output containing private data, or provider responses containing secrets. Use synthetic identities in every proof.

## Session 13 extension: a durable A2A push receiver (A2A track)

### The user-visible capability

S13Code can now delegate a slow unit of work to another A2A agent, park the
owning graph node in `waiting`, release every local worker and connection,
and let the run finish later when a **signed webhook** arrives — even if this
process restarted in between, and even if an at-least-once transport
redelivers that webhook. Before this change the only way to resume a waiting
node was `A2AGraphBridge`'s gRPC `SubscribeToTask` (`official.py`), which
must hold a live stream open for the entire remote task and therefore cannot
survive a restart; the asynchronous-push mode from section 13 had a sender
(`A2ADemoServer._notify` already signed and idempotency-keyed its outbound
pushes) but no receiver. `core/a2a_adapter/push_receiver.py` supplies it:
`PushCorrelationLedger` durably maps a remote `task_id` back to a local
`(run_id, node_id)` and stores the delivered artifact, and
`DurablePushReceiver.handle()` verifies the HMAC-SHA256 signature and bearer
token, **atomically claims the `Idempotency-Key` before touching anything
else**, and only then resumes the node and continues the run. A new
`remote_report` planner mode dispatches from inside the planner, so the node
enters `waiting` without ever having been `running` — future nodes still do
not exist before their inputs.

### The exact API request

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course", "project_id": "s13",
    "user_id": "student-01", "agent_id": "assistant",
    "prompt": "Slow remote report: explain in two lines why an agent card is not permission to access local memory."
  }'
```

This returns in ~5s with the graph unfinished and `remote_specialist` in
`waiting`. The run is then completed out-of-band by the webhook.

### The graph and ordered event trace

Real run `run-0cf4b48bb0c4` against a locally running `glc_v3` + Ollama.
Sequence numbers are global across the journal, so the gap at 3–10 is the
*remote* leg's own run recording its events in the same store.

```
   1  run_started
   2  graph_patched     add=[remote_specialist], wait=[remote_specialist]
                        reason: dispatched to remote A2A agent; parked pending durable push
        -> HTTP response returns here. remote_specialist: waiting, never `running`.
        -> push #1 arrives carrying state="working": acknowledged as progress,
           NOT resumed, so it leaves no graph event (see the ack branch in handle()).
        ... a process restart here changes nothing: node state and correlation are both on disk ...
  11  a2a_push_received remote_specialist   {"remote_task_id": "de0424e9-...", "state": "completed"}
  12  graph_patched     resume=[remote_specialist]
                        reason: durable A2A push resumed waiting node
  13  run_resumed
  14  task_started      remote_specialist
  15  task_succeeded    remote_specialist   (reads the already-delivered artifact; makes no network call)
  16  graph_patched     add=[answer], connect=[remote_specialist -> answer]
                        reason: remote A2A artifact returned; synthesizing final answer
  17  task_started      answer
  18  task_succeeded    answer
  19  graph_patched     finish=true         reason: grounded answer produced
```

Two webhooks were delivered for this run (confirmed in the uvicorn access
log: two `POST /v1/a2a/push` entries) and exactly one produced a graph
mutation. Progress and completion are distinguished by the receiver, not by
the planner.

### The actual final result

`GET /v1/agent/runs/run-0cf4b48bb0c4` → `nodes.answer.result`:

```json
{
  "answer": "An agent card does not grant permission to access local memory because it lacks the necessary authorization for such actions, and accessing local memory requires specific permissions or credentials beyond what an agent card provides. This is due to the absence of any matching authorized durable memory that would allow such access. [source: a2a://de0424e9-f266-4f50-bbb7-f3cf437b82e6]",
  "provider": "ollama",
  "model": "phi4:latest",
  "evidence_count": 1
}
```

### Evidence and provider/agent assignments

| Node | agent | provider / model | evidence contributed |
|---|---|---|---|
| `remote_specialist` | `remote_specialist` | `a2a:remote` (remote task `de0424e9-…`, state `completed`) | kind `a2a_artifact`, source `a2a://de0424e9-…` |
| `answer` | `answer_with_evidence` | `ollama` / `phi4:latest` | final grounded answer, 1 evidence item |

The answer cites the A2A artifact URI rather than presenting it as a local
fact — remote output enters as *untrusted evidence with provenance*, never as
a user statement or an instruction.

**Transport note, stated precisely:** in this single-process demo the
outbound dispatch reaches the peer in-process over ASGI (`httpx.ASGITransport`),
while the **inbound webhook travels over a real loopback TCP socket** to
`http://127.0.0.1:8113/v1/a2a/push` — which is why it appears in the uvicorn
access log. Setting `S13_A2A_REMOTE_URL` to another host makes both legs
ordinary network calls with no code change.

### Required A2A proofs, and where each is proven

| Required proof | Test |
|---|---|
| Discovery | `test_discovery_negotiates_a_mutually_supported_binding_and_version` |
| Version / binding negotiation | same test: picks JSONRPC/1.0 over a server-preferred gRPC entry; raises `AgentCardError` on a 9.9-only card |
| Authentication failure | `test_tampered_signature_is_rejected`, `test_missing_bearer_token_is_rejected` (route returns 401) |
| Task progress | `test_remote_cancellation_...` asserts the pre-terminal `working` state; the live trace shows the `working` push ack'd without resuming |
| Completion artifact | `test_signed_push_durably_resumes_a_waiting_node_and_continues_the_graph` |
| Cancellation | `test_remote_cancellation_propagates_and_its_push_carries_the_canceled_state` (remote `tasks/cancel` + `tasks/get`), and `test_push_arriving_after_local_cancellation_does_not_resurrect_the_node` |
| Duplicate push suppression | `test_a_replayed_webhook_is_suppressed_not_reapplied` |
| Process restart | `test_a_push_resumes_a_waiting_node_after_a_full_process_restart` (graph store *and* ledger reopened from disk) |
| Remote cannot read local memory | `tests/test_s13_a2a_route.py::test_an_inbound_a2a_task_cannot_reach_local_memory_outside_its_task_context` |

### The adversarial failure and its fix

**Attack: a replayed webhook.** `test_before_the_fix_check_then_act_on_state_alone_is_racy`
reproduces what a simpler receiver does — one that only asks *"is the node
still waiting?"* before resuming. That is safe for two strictly sequential
deliveries, but at-least-once transports do not promise that: a redelivery
can reach a second worker while the first is still in flight. Both observe
`waiting` before either commits, and the loser's write is rejected:

```
GraphMutationError: can only resume waiting task remote, got pending
```

The fix is `PushCorrelationLedger.claim()`, a single atomic
`INSERT OR IGNORE` on the `Idempotency-Key` executed *before* the graph is
read or written, so only one caller can ever win a given delivery.
`test_a_replayed_webhook_is_suppressed_not_reapplied` then replays the
identical signed webhook against the real receiver and gets:

```json
{"status": "duplicate", "task_id": "remote-task-1"}
```

with the continuation not re-invoked and the node still in exactly one
resume. `test_duplicate_suppression_survives_process_restart` and
`test_a_push_resumes_a_waiting_node_after_a_full_process_restart` show the
same suppression holding after the ledger is reopened from disk.

**Second attack: memory exfiltration across the A2A boundary.** The claim
"an agent card is not permission to access local memory" is worth nothing if
only asserted in prose, so it is tested. A synthetic fact
(`CLASSIFIED-BUDGET-…`) is written under tenant `course`, then a remote agent
asks for it over the real inbound `/a2a` surface. The fake gateway used by
that test is deliberately **maximally leaky** — it echoes its entire evidence
block into the artifact — so nothing but real scope enforcement can keep the
secret out. A positive control first proves the fact *is* retrievable inside
its own scope, ruling out a vacuous pass. To confirm the test has teeth, the
scope pin in `handle_a2a_task` was temporarily mutated to the local user's
scope; the test failed exactly as it should:

```
AssertionError: assert 'CLASSIFIED-BUDGET-9f3a2b-DO-NOT-EXFILTRATE' not in 'ECHOED EVIDENCE >>> ...'
  'CLASSIFIED-BUDGET-...' is contained here:
    budget is CLASSIFIED-BUDGET-9f3a2b-DO-NOT-EXFILTRATE. [source: chat://student-01/1]
```

The mutation was reverted; enforcement lives in `MemoryStore._scope_where`
(`tenant_id=?` as SQL equality), not in a prompt.

### Honest limitation

This machine's `glc_v3` has only an Ollama route configured (`phi4:latest`,
14.7B, running on CPU — `size_vram: 0`, roughly 100s per completion) and no
Gemini keys, so the five-key routing from section 4 is not exercised here.
Because an A2A-delegated run chains two completions, the stock 120s
`GatewayClient` budget was marginal and two earlier live runs recorded a
genuine `ReadTimeout` on the *downstream synthesis* — never on the push path.
That is why `S13_GATEWAY_TIMEOUT_SECONDS` is now configurable. Across every
live run the dispatch → waiting → signed webhook → atomic resume → artifact
sequence behaved identically; the variance was entirely in local model
latency, and the graph recorded those timeouts as ordinary `task_failed`
events rather than concealing them. A second limitation worth naming: the
demo's "remote" peer is this same service, so it proves the *protocol and
trust boundary* but not cross-organization identity, key rotation, or
revocation — those are the federation concerns listed in section 15 and are
out of scope here.

### Reproduce from a fresh checkout

```bash
git clone <this-fork> && cd S13Code
uv sync

# Adapter + route proofs, including both adversarial tests.
# No glc_v3 and no Ollama required — these are hermetic.
uv run pytest -q s13code/core/a2a_adapter/tests/test_push_receiver.py tests/test_s13_a2a_route.py -v

# Full suite and lint
uv run ruff check .
uv run pytest -q
```

For the live end-to-end run, start `glc_v3` (see its README) and Ollama
first, then:

```bash
ollama pull phi4 && ollama pull nomic-embed-text
export GLC_BASE_URL=http://127.0.0.1:8111
export S13_GATEWAY_PROVIDER=ollama          # or gemini, if glc_v3 has Gemini keys
export S13_SANDBOX_ROOT="$PWD/sandbox"
export S13_CHUNK_MODEL=phi4:latest
export S13_LIVE_SEMANTIC_CHUNKING=1
export S13_A2A_PUSH_SIGNING_SECRET=change-me-shared-webhook-secret
export S13_A2A_PUSH_RECEIVE_TOKEN=change-me-receiver-bearer-token
export S13_GATEWAY_TIMEOUT_SECONDS=600      # two chained completions on a CPU model
uv run s13code serve

# In another shell: dispatch, then watch the node sit in `waiting`.
curl -s http://127.0.0.1:8113/v1/agent/runs -H 'Content-Type: application/json' -d '{
  "tenant_id": "course", "project_id": "s13", "user_id": "student-01", "agent_id": "assistant",
  "prompt": "Slow remote report: explain in two lines why an agent card is not permission to access local memory."
}'

# Poll until the webhook resumes it and the run finishes.
curl -s http://127.0.0.1:8113/v1/agent/runs/<run_id>
```

All identities above are synthetic. The secrets shown are placeholders; no
`.env`, credential, or real user memory is committed.

## License

MIT. See `LICENSE`.

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

Fork the official [`theschoolofai/S13Code`](https://github.com/theschoolofai/S13Code) repository linked from Axiom, create a branch, implement one meaningful extension, and open one pull request against that repository. Do not open the Session 13 pull request against [`theschoolofai/glc_v3`](https://github.com/theschoolofai/glc_v3).

Add one subsection to this README in the same pull request. It must contain:

1. the user-visible capability,
2. the exact prompt or API request,
3. the graph and ordered event trace,
4. the actual final result,
5. evidence and provider/agent assignments,
6. the adversarial failure and its fix,
7. commands that reproduce the result from a fresh checkout.

Do not commit `.env`, credentials, personal memory, generated databases, unrestricted local paths, benchmark output containing private data, or provider responses containing secrets. Use synthetic identities in every proof.

## Session 13 extension: HTML extraction and a labelled retrieval benchmark (semantic indexing track)

### The user-visible capability

S13Code can now index a saved web page. Previously `prepare_markdown`
recognised exactly one document type -- the arXiv abstract page -- and
returned everything else byte-for-byte, so a crawled HTML article reached
Rohan V2 with its navigation, cookie banner, script bodies, sponsor sidebar
and footer intact, and those wrapper words were embedded as though they were
the author's argument. `core/memory/extraction.py` adds a dependency-free
extractor (CPython's `html.parser`, no new supply-chain surface) that keeps
the `<article>`/`<main>` region, drops boilerplate subtrees *and* text hidden
with `display:none`, `visibility:hidden`, `hidden` or `aria-hidden`, and
renders headings, lists and code as Markdown so the existing structural logic
still applies. This is purely an **extraction** change: section 8's rule is
that extraction decides what is content while chunking decides what belongs
together, and Rohan V2's suffix-rollover algorithm is not modified by a single
line. `core/memory/retrieval_benchmark.py` then measures what that is worth
on sixteen labelled queries against two fixed-word controls.

### The exact API request

The page is committed as a synthetic fixture at `sandbox/pages/indexing.html`,
so this request is copy-pasteable verbatim -- no elided body, no local path.

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "s13",
    "user_id": "student-01",
    "agent_id": "assistant",
    "prompt": "Index the file pages/indexing.html and tell me which index type uses the least memory."
  }'
```

The fixture is a saved article wrapped in the chrome a real crawl returns:
`<nav>`, a cookie `<header>`, an analytics `<script>`, a styling `<style>`, a
sponsored `<aside>`, a `<footer>`, and -- planted for the adversarial case --
a `display:none` prompt injection and a `hidden` SEO-stuffing span.

Indexing the same page directly, without the graph, is
`POST /v1/agent/documents` with `{"source_uri": ..., "text": "<the file>"}`.

### The graph and ordered event trace

Real run `run-c5a193cd398e` against a running `glc_v3` + Ollama. Extraction
happens inside the `index_file` node, so the live graph is the honest place to
read this feature's trace.

```
nodes : index_file(succeeded)  recall(succeeded)  distill(failed)  answer(succeeded)
edges : index_file->recall  index_file->answer  recall->distill  recall->answer

  1  run_started
  2  graph_patched    add=[index_file]           reason: first frontier selected for index_file
  3  task_started     index_file
  4  task_succeeded   index_file                 252 source words -> 152 indexed, 100 excluded, 3 chunks, v1
  5  graph_patched    add=[recall]               reason: file is indexed; retrieval can now inspect it
  6  task_started     recall
  7  task_succeeded   recall                     3 document_chunk hits, all from the current version
  8  graph_patched    add=[distill]              reason: retrieved paper evidence is ready for extraction
  9  task_started     distill
 10  task_failed      distill                    ReadTimeout   (local CPU model; failure kept in the journal)
 11  graph_patched    add=[answer]               reason: specialist synthesis completed
 12  task_started     answer
 13  task_succeeded   answer
 14  graph_patched    finish=true                reason: grounded answer produced
```

`recall` does not exist until `index_file` has actually succeeded -- the node
is added by the patch at event 5, in response to the outcome at event 4. The
`distill` timeout at event 10 is retained rather than hidden, and the run still
reaches a grounded answer, which is the section 16 behaviour.

The `index_file` manifest committed by event 4:

```
preprocessing=html_article   landmark=<main>   document_version=1   idempotent=false
252 source words -> 152 indexed words   (100 wrapper words excluded)

 #0  [58w]  chars   0-319   outcome=suffix_rollover   heading="Choosing an approximate index"
 #1  [50w]  chars 319-601   outcome=one_topic         heading="When memory is tight"
 #2  [44w]  chars 603-887   outcome=one_topic         heading="Rebuilding after writes"
```

Nothing from the wrapper survives into those three chunks. Checked explicitly
against the committed text: `IGNORE ALL PREVIOUS…`, `CHEAPVECTORS-SEO-STUFFING`,
`UA-SYNTHETIC-001`, `Pricing`, `cookies`, `Sponsored`, `Copyright` and
`font-weight` are all absent.

### The actual final result

The three chunks retrieved by the `recall` node, in rank order:

```
1. # Choosing an approximate index — HNSW builds a navigable small world graph and reaches high recall…
2. ## When memory is tight — IVF partitions the vector space into cells and probes only the nearest ones…
3. ## Rebuilding after writes — An index is a rebuildable cache and never the source of truth…
```

The answer worker (`ollama` / `llama3.2:3b`) then produced:

> Based on the provided evidence, HNSW uses less memory than IVF because it
> builds a navigable small world graph and reaches high recall at modest memory
> cost. The exact memory usage of each index type is not explicitly stated in
> the provided documents. However, the fact that HNSW is mentioned as the
> default choice for corpora that fit in RAM suggests that it may use less
> memory than IVF, which is noted to "use markedly less memory"… Therefore, I
> can only make an inference about the relative memory usage.

**This answer is wrong, and it is reported here unedited.** The fixture says
IVF "uses markedly less memory"; the 3B model quoted that clause and still
concluded the opposite. What did work is everything this PR is responsible for:
extraction removed the chrome, the boundary-aware split kept "markedly less
memory" intact inside chunk #1, and retrieval put that chunk in the top three.
The model even labelled its own claim an inference rather than asserting it as
stated, which is the stated/derived/inferred distinction from section 6 doing
its job. Retrieval quality and synthesis quality are separate properties, and a
14B-class model is not available on this box -- see the limitations below.

### Evidence and provider/agent assignments

| Node | agent | provider / model | contribution |
|---|---|---|---|
| `index_file` | `index_file` | none -- deterministic | extraction + chunking + atomic ingest, v1, 3 chunks |
| `recall` | `memory_recall` | `nomic-embed-text` (embeddings) | 3 scoped `document_chunk` hits, current version only |
| `distill` | `paper_distiller` | `ollama` — timed out | failed, retained in the journal |
| `answer` | `answer_with_evidence` | `ollama` / `llama3.2:3b` | final grounded answer, cites the indexed source |

| Stage | Component | Provider / model |
|---|---|---|
| Extraction | `extraction.py` | none -- CPython `html.parser`, deterministic |
| Segmentation | `semantic_chunks` (unmodified) | `heading_fallback` for this run (see limitations) |
| Embedding | `OllamaNomicEmbedder` | `nomic-embed-text`, `search_document:` / `search_query:` prefixes |
| Storage | `MemoryStore.ingest_document` | SQLite source of truth + rebuildable FAISS sidecar |

Every chunk keeps `source_start_char`/`source_end_char` into the prepared text,
both `source_sha256` and `prepared_sha256`, its segmentation outcome, and the
`preprocessing` label `html_article`, so a disputed passage is traceable to the
revision that produced it. The `source_uri` recorded for this run is the
sandbox-relative fixture resolved through `S13_SANDBOX_ROOT`; no path outside
that sandbox is reachable by a prompt.

### Required semantic-indexing proofs

| Required proof | Test |
|---|---|
| Exact source coverage | `test_chunks_tile_the_prepared_document_with_no_loss_and_no_overlap` — walks the spans, restores inter-span whitespace, and asserts the result equals the prepared document **character for character** |
| No overlap | same test: each chunk starts at or after the previous chunk's end, and the gap between any two chunks is whitespace only |
| Idempotent re-indexing | `test_reindexing_an_unchanged_page_is_idempotent` (+ live `idempotent=true` above) |
| Rollback on injected failure | `test_injected_failure_rolls_back_and_leaves_the_previous_version_active` |
| Better retrieval on ≥10 labelled queries | 16 queries. `test_measured_retrieval_beats_both_fixed_word_controls` asserts the table above against **both** controls with real embeddings (opt-in, passes in 267s); `test_semantic_boundaries_preserve_more_answer_spans_than_the_fixed_word_control` checks the underlying mechanism hermetically |
| Suffix-rollover invariant unchanged | `chunking.py` diff is +15/−2 and touches only the import, the extraction hand-off, and a configurable timeout |

Labels name the *answering text*, never a chunk boundary, so no label can be
satisfied only by the strategy that drew that boundary.

### The adversarial failure and its fix

**Attack: smuggling text into the index through invisible HTML.**
`test_hidden_html_cannot_smuggle_text_into_the_index` is a single test with
both halves in it. A page carries an ordinary visible article plus an
injection in a `display:none` div, an SEO-stuffed `hidden` span, an
`aria-hidden` decoration, and a `<script>` whose body reads like prose.

*Before* — the test first chunks that page **without** extraction, which is
exactly what upstream `prepare_markdown` did with any HTML input, and asserts
the attack succeeds:

```python
assert INJECTION in smuggled   # precondition: raw HTML really does carry it
assert STUFFED  in smuggled
```

Those hidden sentences would have been embedded and could later be retrieved
into an answer worker's evidence, even though no human reading the page could
see them.

*After* — the same page through the real path:

```python
prepared, label = prepare_markdown(HOSTILE_PAGE)
assert label == "html_article"
assert INJECTION not in prepared
assert STUFFED   not in prepared
assert "Recall at k measures" in prepared                 # article intact
assert "Latency should be reported at the tail" in prepared
```

That last assertion caught a real bug during development. The first
implementation tracked dropped elements with a counter keyed on element
*name*, which cannot tell which `</div>` closes a hidden `<div>` — so every
paragraph *after* the injection was silently swallowed. The fix was a proper
tag stack (`_drop_at` records the stack depth where dropping began), and the
assertion above is what keeps it honest.

A second adversarial case,
`test_stale_document_version_is_never_retrievable_after_reindex`, re-indexes
a changed page and asserts the superseded v1 sentence can no longer answer a
question while remaining visible to audit as `status="superseded"`.

### Honest limitations

**The measured run used `heading_fallback`, not live phi4.** This machine has
no GPU, and phi4 (14.7B, `size_vram: 0`) exceeded the segmenter's built-in
180s ceiling on a single 300-word block — one call did not return in over ten
minutes, which would have silently degraded every block to
`segmenter_failed_fallback_to_block` and produced a table that said
"semantic" while measuring nothing of the kind. Rather than publish that, the
benchmark was run with the repository's deterministic heading-boundary mode
and real Nomic embeddings. The claim under test — *boundary-aware placement
retrieves better than blind word counts* — is genuinely measured; the claim
*phi4 specifically chooses good boundaries* is **not** measured here and is
not asserted. `S13_CHUNK_TIMEOUT_SECONDS` was added so a slower box can raise
that ceiling deliberately instead of degrading in silence. I also tried
`llama3.2:3b` as a faster segmenter: it answered in 14–36s but returned a
*paraphrased* suffix, which the existing no-hallucination fence correctly
rejected — a small independent confirmation that the fence works.

**The synthesis model is small, and it got the answer wrong.** The live run
above was served by `llama3.2:3b`, because phi4 exceeds the gateway's 120s
per-call budget on this CPU. It misread its own evidence, as shown verbatim in
the result section. Retrieval put the correct passage in the top three; the
model reasoned over it poorly. This PR changes extraction and measures
retrieval, and neither claim depends on the synthesis step -- but presenting a
clean-looking answer here would have hidden a real weakness of the local setup,
so the wrong one is printed instead. The `distill` node's `ReadTimeout` in the
same run has the same cause.

**Corpus scale.** Four pages, 1201 extracted words, 18 semantic chunks. That
is enough for sixteen labelled queries to separate the strategies, but the
differences against the size-matched control are single-query steps, so the
±0.06 recall@1 gap should be read as directional rather than precise.

**Extraction is normalisation, not slicing.** Unlike the arXiv path, whose
prepared text is a substring of the source, HTML extraction rewrites tags into
Markdown, so chunk spans are exact into the *prepared* document rather than
into the raw HTML. Both hashes are recorded, and the excluded word count is
reported, so the relationship stays inspectable.

### Reproduce from a fresh checkout

```bash
git clone <this-fork> && cd S13Code
uv sync

# All proofs above: extraction, coverage, no-overlap, idempotency, rollback,
# both adversarial cases, and the benchmark mechanism. Hermetic -- no glc_v3,
# no Ollama, no network.
uv run pytest -q tests/test_html_semantic_indexing.py -v

# Full suite and lint
uv run ruff check .
uv run pytest -q
```

Verifying the measured table needs Ollama only for embeddings. The benchmark
is opt-in because it takes about four and a half minutes; the assertion inside
it is that semantic beats **both** controls, so it fails rather than passes if
the claim in this README stops holding:

```bash
ollama pull nomic-embed-text
S13_RUN_RETRIEVAL_BENCHMARK=1 uv run pytest -q \
  tests/test_html_semantic_indexing.py::test_measured_retrieval_beats_both_fixed_word_controls -v
```

To print the table itself rather than assert on it:

```bash
uv run python -c "
from s13code.core.memory.retrieval_benchmark import run, format_report
from s13code.core.memory.embeddings import OllamaNomicEmbedder
from s13code.core.memory.chunking import HeadingTopicSegmenter
print(format_report(run(OllamaNomicEmbedder(), segmenter=HeadingTopicSegmenter())))
"
```

And the live graph run that produced the trace above (needs `glc_v3` plus
Ollama). The fixture it indexes is committed, so nothing else has to be
prepared:

```bash
ollama pull nomic-embed-text
export GLC_BASE_URL=http://127.0.0.1:8111
export S13_GATEWAY_PROVIDER=ollama
export S13_SANDBOX_ROOT="$PWD/sandbox"
export S13_LIVE_SEMANTIC_CHUNKING=0     # heading boundaries; set 1 for live phi4
uv run s13code serve &

curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "s13",
    "user_id": "student-01",
    "agent_id": "assistant",
    "prompt": "Index the file pages/indexing.html and tell me which index type uses the least memory."
  }'
```

The response carries the graph, the ordered event trace, the `index_file`
manifest and the provider/agent assignments quoted above. Re-running the same
request re-indexes idempotently (`idempotent: true`, version unchanged).

All identities in the tests and proofs are synthetic; no `.env`, credential,
personal memory, or absolute local path is committed.

## License

MIT. See `LICENSE`.

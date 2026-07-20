"""A labelled retrieval benchmark: Rohan V2 boundaries vs a fixed-word control.

Section 9 argues that chunking asks "where did the author stop discussing one
retrievable subject?" while embedding similarity only answers "which meanings
are near one another?". That is a claim about retrieval quality, so it should
be measured rather than asserted.

The corpus is deliberately saved *HTML pages* so the run also exercises the
extractor in ``extraction.py``: every strategy below receives exactly the same
extracted text, which isolates the variable under test to boundary placement.

Two controls, not one. ``fixed_word_chunks`` at its library default of 40
words is much smaller than a typical semantic chunk, so beating it could just
mean "bigger chunks retrieve better". The second control therefore uses a
fixed size matched to the mean semantic chunk length, which leaves boundary
*placement* as the only difference. A result that only beats the first
control would be reported here as inconclusive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .chunking import fixed_word_chunks, prepare_markdown, semantic_chunks
from .embeddings import Embedder


@dataclass(frozen=True)
class LabeledQuery:
    """A question plus the phrase whose presence makes a chunk a correct hit.

    Marker-based labelling is strategy-neutral: it never mentions a boundary,
    so it cannot be tuned to favour either chunker.
    """

    query: str
    marker: str


@dataclass(frozen=True)
class StrategyResult:
    name: str
    chunks: int
    mean_words: float
    recall_at_1: float
    recall_at_3: float
    mrr: float

    def row(self) -> str:
        return (f"{self.name:<28} {self.chunks:>6} {self.mean_words:>11.1f} "
                f"{self.recall_at_1:>9.2f} {self.recall_at_3:>9.2f} {self.mrr:>7.3f}")


# --- corpus -----------------------------------------------------------------
# Saved pages in the shape real crawls produce: a landmark region wrapped in
# navigation, cookie chrome, analytics script and a footer.
def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>.masthead {{ font-weight: 600 }}</style>
<script>window.analyticsId = "UA-000000"; function trackPageview() {{ }}</script>
</head><body>
<nav><a href="/">Home</a> <a href="/guides">Guides</a> <a href="/pricing">Pricing</a> <a href="/contact">Contact</a></nav>
<header><h1>Platform Engineering Handbook</h1>
<p>We use cookies and similar technologies to personalise content. Accept all or manage preferences.</p></header>
<main>
{body}
</main>
<aside><h3>Sponsored</h3><p>Upgrade to the enterprise tier for unlimited seats and priority support.</p></aside>
<footer><p>Copyright 2026 Platform Engineering Handbook. All rights reserved. Terms of service. Privacy policy.</p></footer>
</body></html>"""


CORPUS: dict[str, str] = {
    "https://example.test/guides/indexing": _page("Indexing", """
  <h1>Approximate indexes</h1>
  <p>An approximate nearest neighbour index trades exactness for latency. The
     practical question is never whether it is exact, but how much recall you
     surrender per millisecond saved. HNSW builds a navigable small world graph
     and reaches high recall at modest memory cost, which is why it has become
     the default choice for corpora that fit comfortably in RAM. Its build time
     grows with the number of links per node, so an index that is cheap to
     query can still be expensive to construct on a large corpus.</p>
  <p>IVF partitions the vector space into cells and searches only the closest
     ones. It uses markedly less memory than a graph index, so it suits very
     large corpora, but recall falls sharply when the number of probed cells is
     set too low. Tuning nprobe is therefore the single most important knob,
     and the correct value depends on how evenly the corpus is distributed
     across cells rather than on the corpus size alone.</p>
  <h2>Quantisation</h2>
  <p>Product quantisation compresses each vector into a short code, cutting
     memory by an order of magnitude. The cost is a systematic loss of
     precision at short distances, which matters most when the top candidates
     are close together. A common remedy is to over-fetch candidates with the
     compressed index and then rescore the survivors against the full precision
     vectors, paying a little latency to recover the ordering.</p>
  <h2>Rebuilding after writes</h2>
  <p>An index is a rebuildable cache, never the source of truth. When the
     authoritative store changes, the index must be rebuilt from the current
     authorised records rather than patched in place, because a partially
     patched index silently returns records that no longer exist. Rebuilds
     should be cheap enough to run routinely; if a rebuild is frightening, the
     system has already drifted into treating the cache as authoritative.</p>
  <h2>Measuring quality</h2>
  <p>Report recall at k against an exhaustive scan of the same corpus. A single
     averaged score hides the tail, so publish the distribution as well; the
     queries that fail are the ones your users will remember. Latency should be
     reported at the ninety ninth percentile, because the mean is dominated by
     the easy queries that were never at risk.</p>
"""),
    "https://example.test/guides/chunking": _page("Chunking", """
  <h1>Splitting documents for retrieval</h1>
  <p>A fixed word count is the simplest possible splitter and the easiest to
     reason about. It is also indifferent to meaning: it will cut through the
     middle of a definition, leaving the term in one chunk and its explanation
     in the next, so neither chunk answers the question on its own. The damage
     is invisible in aggregate statistics and obvious the moment a user asks
     the one question whose answer straddled a boundary.</p>
  <p>Overlapping windows are the usual patch. Overlap raises the chance that a
     definition survives intact, but it inflates the index, bills you twice for
     the same tokens, and returns near duplicate passages that crowd out other
     documents from the result list. Overlap treats the symptom, since the
     boundary is still placed without any reference to the subject matter.</p>
  <h2>Topic boundaries</h2>
  <p>A boundary-aware splitter asks a different question: has the author
     stopped discussing one retrievable subject and started another? Because
     the boundary lands where the subject changes, a definition and its
     explanation stay together and no overlap is required at all. The splitter
     needs only to identify where a new subject begins, which is a far easier
     judgement than summarising or rewriting the passage.</p>
  <h2>Guarding against invention</h2>
  <p>Any splitter that asks a language model where to cut must verify the
     answer. If the model paraphrases the text instead of quoting it, the
     proposed boundary is rejected and the block is kept whole, because a
     rewritten passage is no longer the author's words and can no longer be
     cited. Verification is a string comparison, not another model call.</p>
  <h2>Very short blocks</h2>
  <p>Below roughly forty words the cost of asking for a boundary exceeds the
     likely retrieval gain, so a short trailing section is kept as a single
     chunk. Recording that decision matters: a reader of the manifest can then
     distinguish a deliberate floor from a model that saw only one topic.</p>
  <h2>Verifying a split</h2>
  <p>Whatever the strategy, the chunks must tile the prepared document exactly:
     concatenating them in order reproduces the source with no text lost and no
     text duplicated. That property is checkable without a language model and
     should be a test, not a hope.</p>
"""),
    "https://example.test/guides/provenance": _page("Provenance", """
  <h1>Provenance and versioning</h1>
  <p>A retrieved passage without a source is a rumour. Every chunk should carry
     the document it came from, its character span, and the hash of the exact
     bytes that were indexed, so a disputed answer can be traced back to the
     revision that produced it. Without the hash there is no way to tell
     whether the passage came from the revision under discussion.</p>
  <h2>Atomic visibility</h2>
  <p>Indexing can fail halfway through. Retrieval must therefore never observe
     a half written version: stage the whole document version inside a
     transaction and make it visible only on commit, so a failure leaves the
     previous complete version active and unchanged. Partial visibility is the
     worst outcome, because the system looks healthy while quietly answering
     from an incomplete document.</p>
  <h2>Superseding a version</h2>
  <p>When the source changes, write a new version and mark the previous one
     superseded rather than deleting it. Normal recall then sees only current
     chunks, while an audit can still explain what the system believed at the
     time it answered, which is the difference between a debuggable system and
     an unaccountable one.</p>
  <h2>Idempotent reindexing</h2>
  <p>Reindexing an unchanged source must not create a second copy. Comparing
     the content hash of the prepared text against the active version lets the
     indexer return the existing version untouched, which keeps a nightly crawl
     from multiplying the corpus every time it runs.</p>
  <h2>Neighbour expansion</h2>
  <p>Expanding a hit to its adjacent chunks adds useful context, but adjacency
     is not permission: the neighbours must come from the same current version
     and pass the same scope check as the hit itself.</p>
"""),
    "https://example.test/guides/evaluation": _page("Evaluation", """
  <h1>Evaluating a retrieval change</h1>
  <p>A retrieval change should be judged on a labelled set, not on the example
     that prompted it. Collect the questions users actually asked, mark the
     passage that answers each one, and keep the set fixed while you iterate,
     because a benchmark edited alongside the code stops measuring anything.</p>
  <h2>Choosing a control</h2>
  <p>The control must differ from the treatment in exactly one respect. If a
     new splitter produces larger chunks than the baseline, then a win may
     simply reflect chunk size, so the honest comparison adds a second control
     with the size held constant. Without that, an improvement claim is
     confounded and should be reported as inconclusive.</p>
  <h2>Metrics worth reporting</h2>
  <p>Recall at one rewards putting the answer first, while recall at three
     reflects what a reader will actually skim. Mean reciprocal rank summarises
     both, but it should never be reported alone, since a single number cannot
     show whether a change helped the median query or only the easy ones.</p>
  <h2>Labelling without bias</h2>
  <p>Labels should name the answering text, never a chunk identifier. A label
     that refers to a boundary can only be satisfied by the strategy that drew
     that boundary, which quietly guarantees the result you hoped for.</p>
"""),
}


# Sixteen questions a reader of the corpus would actually ask -- above the ten
# the brief requires, because twelve-odd binary outcomes move in coarse steps.
# Each marker names the *answering text*, never a chunk boundary, so no label
# can be satisfied only by the strategy that drew that boundary.
QUERIES: tuple[LabeledQuery, ...] = (
    LabeledQuery("which index type uses the least memory for very large corpora?",
                 "markedly less memory"),
    LabeledQuery("what happens to recall if nprobe is too low?",
                 "recall falls sharply"),
    LabeledQuery("how do I recover ordering lost to compressed vectors?",
                 "rescore the survivors"),
    LabeledQuery("why should a vector index be rebuilt instead of patched?",
                 "patched index silently returns records"),
    LabeledQuery("which latency percentile should I report?",
                 "ninety ninth percentile"),
    LabeledQuery("how should I report retrieval quality?",
                 "publish the distribution"),
    LabeledQuery("what goes wrong when you split on a fixed word count?",
                 "cut through the middle of a definition"),
    LabeledQuery("what is the downside of overlapping windows?",
                 "bills you twice"),
    LabeledQuery("what question does a boundary-aware splitter ask?",
                 "stopped discussing one retrievable subject"),
    LabeledQuery("what happens if the model paraphrases instead of quoting?",
                 "proposed boundary is rejected"),
    LabeledQuery("why are very short trailing sections left unsplit?",
                 "cost of asking for a boundary exceeds"),
    LabeledQuery("how do I verify that a document was split correctly?",
                 "tile the prepared document exactly"),
    LabeledQuery("what should a chunk carry so an answer can be traced?",
                 "hash of the exact bytes"),
    LabeledQuery("how do I stop retrieval seeing a half written document?",
                 "visible only on commit"),
    LabeledQuery("what stops a nightly crawl duplicating the corpus?",
                 "return the existing version untouched"),
    LabeledQuery("why does a control need to match the treatment size?",
                 "size held constant"),
)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _rank(chunks: list[str], vectors: list[list[float]], query_vector: list[float]) -> list[int]:
    scored = sorted(range(len(chunks)), key=lambda i: _cosine(vectors[i], query_vector), reverse=True)
    return scored


def evaluate_strategy(name: str, chunks: list[str], queries, embedder: Embedder, *, k: int = 3) -> StrategyResult:
    """Score one chunking strategy over the pooled corpus.

    Documents are pooled rather than scored per file, because the realistic
    failure is a wrong-document hit, not a wrong-chunk-within-a-known-file hit.
    """
    vectors = [embedder.embed_document(chunk) for chunk in chunks]
    normalised = [_normalise(chunk) for chunk in chunks]
    hits_1 = hits_3 = 0
    reciprocal = 0.0
    for item in queries:
        order = _rank(chunks, vectors, embedder.embed_query(item.query))
        marker = _normalise(item.marker)
        found_at = next((rank for rank, index in enumerate(order, start=1)
                         if marker in normalised[index]), None)
        if found_at == 1:
            hits_1 += 1
        if found_at is not None and found_at <= k:
            hits_3 += 1
        if found_at is not None:
            reciprocal += 1.0 / found_at
    total = len(queries)
    words = [len(chunk.split()) for chunk in chunks] or [0]
    return StrategyResult(name=name, chunks=len(chunks), mean_words=sum(words) / len(words),
                          recall_at_1=hits_1 / total, recall_at_3=hits_3 / total, mrr=reciprocal / total)


def build_strategies(embedder: Embedder, *, segmenter=None) -> dict[str, list[str]]:
    """Prepare every corpus page once, then split it three ways."""
    prepared = []
    for source in CORPUS.values():
        text, _label = prepare_markdown(source)
        prepared.append(text)

    semantic: list[str] = []
    for text in prepared:
        semantic.extend(chunk.text for chunk in
                        semantic_chunks(text, embedder, preprocess=False, segmenter=segmenter))

    mean_words = max(1, round(sum(len(c.split()) for c in semantic) / max(1, len(semantic))))
    fixed_default: list[str] = []
    fixed_matched: list[str] = []
    for text in prepared:
        fixed_default.extend(fixed_word_chunks(text, words=40))
        fixed_matched.extend(fixed_word_chunks(text, words=mean_words))
    return {
        "rohan_v2_semantic": semantic,
        "fixed_40_words (library default)": fixed_default,
        f"fixed_{mean_words}_words (size-matched)": fixed_matched,
    }


def run(embedder: Embedder, *, segmenter=None, k: int = 3) -> list[StrategyResult]:
    strategies = build_strategies(embedder, segmenter=segmenter)
    return [evaluate_strategy(name, chunks, QUERIES, embedder, k=k) for name, chunks in strategies.items()]


def format_report(results: list[StrategyResult]) -> str:
    header = f"{'strategy':<28} {'chunks':>6} {'mean_words':>11} {'recall@1':>9} {'recall@3':>9} {'MRR':>7}"
    lines = [header, "-" * len(header)]
    lines.extend(result.row() for result in results)
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - operator entry point
    from .embeddings import OllamaNomicEmbedder

    embedder = OllamaNomicEmbedder()
    results = run(embedder)
    print(format_report(results))
    release = getattr(embedder, "release", None)
    if release:
        release()


if __name__ == "__main__":  # pragma: no cover
    main()

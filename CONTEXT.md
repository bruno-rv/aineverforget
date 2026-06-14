# Context: aineverforget

aineverforget is a local-first personal knowledge base. It ingests heterogeneous
text inputs (notes, transcripts, summaries, PDFs, codebase summaries — more types
later), indexes them in a local vector store, and answers questions grounded in
the whole corpus with citations. Inspired by the `neverforget` lesson-recorder
pipeline, but generic: it does not capture Sources itself — it indexes whatever
content you supply and journals what it does with it.

This file is a glossary of the project's ubiquitous language. It holds no
implementation detail. Implementation lives in `PLAN.md` and `docs/adr/`.

---

## Glossary

### Source
A piece of text handed to the system to remember — the user-facing unit of
"ingest this." A Source may be authored by the user (a note, a transcript, a PDF)
or emitted by a Producer. aineverforget is producer-agnostic: it does not generate
content, it ingests whatever text it is given. A whole codebase, explored by an
outside tool, arrives as an ordinary text Source.

### Producer
An external tool or person that creates content for aineverforget to ingest — a
codebase explorer, a meeting transcriber, the user writing a note. Producers live
outside the system; aineverforget's contract with them is plain text/markdown (and
PDF). New Producers need no change to aineverforget.

### Loader
The component that knows how to read one kind of Source and turn it into
Documents. One Loader per Source type (in v1: text/markdown and PDF). Adding a new
Source type means adding a Loader; nothing else changes. (Registry pattern.)
Codebase information is not its own Loader — it arrives as markdown from a Producer
and is ingested through the text Loader.

### Document
The normalized form of ingested content: plain text plus metadata, extracted by a
Loader from a Source. One Source may yield many Documents — a Producer markdown
bundle (e.g. a codebase summary) produces many Documents.

### Chunk
A retrievable slice of a Document — the atomic unit that is embedded, stored, and
returned by retrieval. A Document is split into one or more Chunks.

### Corpus
Every Document and Chunk the system currently remembers. The total knowledge the
user can ask against.

### Ingest
The act of turning Sources into stored, searchable Chunks: read (Loader) →
normalize (Document) → split (Chunk) → embed → store. Idempotent: re-ingesting
unchanged content is a no-op; changed content replaces its old Chunks.

### Ask
The act of asking a natural-language question and receiving an answer grounded in
the Corpus, with Citations back to the Sources that support it. An Ask is either a
Recall or a Synthesis.

### Recall
An Ask answered from a few directly-relevant Chunks: pinpoint lookup —
"what did I decide about X?", "where's that note on Y?". The simple, fast case.

### Synthesis
An Ask that must draw on many Chunks across many Sources: aggregation and
summarization — "summarize everything I know about X", "how many times did I
mention Y", "list every project touching Z". Cannot be answered from a single
top-k retrieval; requires decomposing the question, retrieving broadly, and
combining the results. A first-class capability, not an afterthought.

### Total Context
The aspiration that an Ask — Recall or Synthesis — can reach the entire Corpus,
across every Source type, rather than one silo. It is an aim the system works
toward, not a guarantee that every answer literally reads every Chunk: a Recall
reaches the most relevant Chunks; a Synthesis reaches broadly across them.

### Citation
A pointer from an answer back to the specific Source/Chunk that supports a claim,
so the user can verify and trace any answer.

---

## Agents & orchestration

The system follows the AgentSpec pattern, the same way `neverforget` does: a thin
deterministic Tool layer driven by judgment-bearing Agents, coordinated by an
Orchestrator. Mechanical work is code; judgment is an Agent.

### Tool layer
The deterministic `aineverforget` command-line program: load, chunk, embed, store,
search, scroll, verify. No judgment, fully reproducible. Agents invoke it; they do
not re-implement it.

### Agent
A single-responsibility specialist (an AgentSpec agent definition) that performs
one judgment-bearing step — summarize notes, retrieve relevant Chunks, synthesize
an answer. Each Agent has a Knowledge Base. An Agent never dispatches another
Agent.

### Knowledge Base
The curated patterns, runbooks, and contracts that govern one Agent's behavior —
e.g. the template a summarizer must follow, the calibration an answer step must
respect. Lives beside the Agent; the binding source of how that step is done.

### Orchestrator
The main Claude session running an aineverforget skill (e.g. `/ingest`, `/ask`).
It is the only thing that dispatches Agents and routes on their results — for
Synthesis it decomposes the question and dispatches the retrieval Agent more than
once, then the answer Agent. One level of nesting only (see ADR).

### Agent Registry
The authoritative list of every Agent and what it owns. Creating an Agent and
registering it happen together; an unregistered Agent does not exist.

---

## Quality & evaluation

Every Agent's output is held to a measurable standard before it is trusted. Two
distinct standards apply — one in the live loop, one in development.

### Quality Gate
A cheap, deterministic check run on an Agent's output *every time it runs* — does
the answer cite real Chunks, do the verify probes pass, did retrieval return
citationable candidates (per-modality hit counts — never a fused-RRF score floor,
which is rank-based and uncalibrated). Failing a Gate makes the step Reiterate.
Gates live in the hot path, so they must be fast and not depend on a judge model.

### Eval
A development-time measurement of an Agent's quality against a versioned gold set
(expected summaries, expected retrievals, expected answers). Runs when an Agent's
prompt, model, or settings change — never in the live loop. May use a cross-model
judge. The gold set is never normalized to the live Corpus.

### Quality Threshold
The minimum score an Agent must reach to be trusted — at a Gate (live) or an Eval
(dev-time). Recorded per Agent in the Agent Registry.

### Reiterate
What a step does when it fails its Gate: retry, then escalate to a stronger model,
and finally escalate to the user. Bounded by the Two-Strike Rule.

### Two-Strike Rule
The same fix for the same symptom is attempted at most twice. The third occurrence
escalates to needs_user. Stops the loop from cycling a failing fix forever.

### needs_user
The state a step reaches when automation cannot pass a Gate within its bounds.
Work stops on that item and surfaces to the user with evidence.

---

## Observability

### Run Journal
The append-only record of what the system did — every Ingest and every Ask as a
sequence of events (what was retrieved, which Agents ran, Gate scores, Reiterates,
model escalations, the final verdict). The single source of truth for "what
happened."

### Cost Telemetry
The dispatches and tokens spent per Ask, recorded so an expensive Ask is visible
even though no hard ceiling stops it. The chosen mitigation for running without a
per-Ask budget cap.

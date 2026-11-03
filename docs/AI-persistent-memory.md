
> **Platform scope update (2026-11-02):** Multi-Agent-AI targets **Windows 10/11 and Linux** with one shared Python orchestration engine and Tkinter control panel. Windows uses a PyInstaller/Inno Setup installer; Linux uses a per-user archive installer. Platform-specific notes below describe implementation details or historical Windows tests, **not a Windows-only product restriction**. Linux provider execution and both installers still require end-to-end verification.

# AI Persistent Memory — Architecture and Implementation Plan

**Project:** Multi-Agent-AI  
**Status:** Proposed design; not implemented  
**Target:** Native Windows 10/11 and Linux, Python >=3.11, VS Code, Claude Code, Codex CLI and Copilot CLI  
**Scope:** Project-neutral persistent knowledge shared by managed agents across tasks, sessions, restarts, and providers.

## 1. Executive summary

Add a persistent memory subsystem to the existing deterministic Python orchestrator. The system must help agents reuse verified knowledge, decisions, procedures, and past task outcomes without replaying complete transcripts. It must not replace the task board, mailbox, job event log, authoritative project rules, or the existing SQLite state index.

**Design principles:** durable and local-first; provider-neutral; scoped and auditable; low token overhead; deterministic lifecycle; evidence-based promotion; privacy by default; safe concurrent access; backward-compatible, optional integration.

**Non-goals for v0.1:** training or modifying foundation-model weights; automatic changes to model prompts without a measured retrieval policy; remote synchronization; autonomous rule changes; inferring acknowledgment, approvals, or task completion from remembered statements; claiming agent consciousness.

## 2. Fit with the existing repository

Current repository design includes:
- Python package `src/multi_agent_ai/`, stdlib-only runtime, Python >=3.11.
- Configurable state root (default `.multi-agent-ai/`) with `state.sqlite3` as an operational/derived index.
- Durable mailbox, explicit recipient acknowledgments, task board, job event log, managed workers, verification, and isolated Git worktrees.
- Strict single-writer ownership for coordination files, explicit approval before main-branch changes, and dry-run-by-default real provider execution.
- A planned but not yet implemented adaptive performance ledger.

The new memory subsystem is **complementary**. Coordination state is authoritative for job status; memory is advisory context. Do not reinterpret mailbox acknowledgments, reviews, or plan revisions as memory writes. Existing protocol grammar and authority boundaries remain unchanged.

## 3. Conceptual architecture

```text
User / VS Code
      |
AI coordinator -- task planner / router
      |
Deterministic Python orchestrator
      |------------------------------------------|
      |              |             |             |
  Claude worker   Codex worker  Copilot worker  Verifier
      |              |             |             |
      +--------------+-------------+-------------+
                     |
           Memory facade / policy gate
             |                  |
     Retrieval & context     Evidence ingestion
       assembly                and review
             |                  |
             +---------+--------+
                       |
          SQLite transactions + FTS5
           |                    |
     Versioned records     Source references
                       |
            Optional vector adapter
```

The orchestrator, not individual LLMs, owns database access and enforces permissions. Workers consume bounded, cited memory snippets. Workers can propose memories, but verification and promotion are separate actions.

## 4. Memory types and scopes

Types:
1. **Decision:** architectural choice and rationale; linked to authoritative decision document; cannot supersede that document.
2. **Fact:** verified repository or environment observation, including source commit/version.
3. **Procedure:** reproducible commands and steps, with applicable platform and verification evidence.
4. **Lesson:** failure mode, successful workaround, limitations, and confidence.
5. **Task outcome:** result, tests, reviewer verdict, provider/model, timing and usage evidence.
6. **Preference/constraint:** user-approved project behavior, linked to its source; never overrides current instructions.
7. **Hypothesis:** unverified agent observation, excluded from high-trust retrieval by default.

Scopes: `job`, `project`, `workspace`, `global`. Default to `project`; global sharing requires explicit policy. Derive stable project identity from an initialized project registry, not from a mutable absolute worktree path. Worktrees of the same repository share project memory, while separate repositories do not. Cross-project retrieval must be explicitly opted into.

Distinguish **short-lived task context** from **long-lived knowledge**. Do not automatically retain entire prompts, raw conversations, secrets, or logs.

## 5. Persistence and schema

For v0.1 use a dedicated `<state-root>/memory.sqlite3`, rather than changing the semantics of the existing derived `state.sqlite3`. Memory is durable source data, not a disposable index. SQLite WAL mode, `busy_timeout`, foreign keys, transactional migrations, short write transactions, and periodic integrity checks are required.

Proposed tables:

```text
memory_records(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  confidence TEXT NOT NULL,  -- hypothesis|reported|verified
  status TEXT NOT NULL,      -- proposed|active|superseded|retracted
  revision INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  expires_at_utc TEXT,
  content_hash TEXT NOT NULL,
  superseded_by TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'internal'
)
memory_sources(
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memory_records(id),
  source_type TEXT NOT NULL, -- job|task|attempt|commit|file|test|user
  source_ref TEXT NOT NULL,
  source_revision TEXT,
  evidence_summary TEXT
)
memory_revisions(
  memory_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  changed_at_utc TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(memory_id, revision)
)
memory_events(
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  event_at_utc TEXT NOT NULL,
  metadata_json TEXT NOT NULL
)
memory_fts -- FTS5 virtual table, rebuilt/maintained transactionally
schema_migrations(version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL)
```

Use parameterized SQL, validate enum values and maximum content lengths, and keep `memory_fts` synchronized on insert/update/retract. Define unique source/deduplication constraints in migration design. `content_hash` helps detect exact duplicates but is not a replacement for semantic consolidation.

Operational state and memory have separate databases; where a task completion and memory extraction span both, use an idempotent ingestion event keyed to the immutable job/attempt identifier and reconcile on restart. Never claim atomic cross-database transactions.

## 6. API and CLI

Proposed internal facade:

```python
MemoryStore.add_proposal(record, sources, actor) -> MemoryId
MemoryStore.get(memory_id, actor) -> MemoryRecord
MemoryStore.search(query, project_id, scopes, filters, limit, actor) -> list[MemoryHit]
MemoryStore.promote(memory_id, expected_revision, evidence, actor) -> MemoryRecord
MemoryStore.supersede(memory_id, replacement_id, expected_revision, actor) -> None
MemoryStore.retract(memory_id, expected_revision, reason, actor) -> None
MemoryStore.history(memory_id, actor) -> list[Revision]
MemoryContext.build(task, token_budget, actor) -> ContextBundle
```

Proposed CLI:
- `multi-agent-ai memory init --state-root PATH`
- `multi-agent-ai memory add --project ID --kind lesson --title ... --content-file PATH --source ...`
- `multi-agent-ai memory search "query" --project ID --limit 10 --json`
- `multi-agent-ai memory show ID --history`
- `multi-agent-ai memory promote ID --evidence ... --expected-revision N`
- `multi-agent-ai memory retract ID --reason ... --expected-revision N`
- `multi-agent-ai memory doctor --state-root PATH`

All mutations must obey scope and actor authorization; destructive actions must have explicit confirmation and audit trails. Dry-run behavior should be consistent with the existing CLI where applicable. Commands must not invoke paid providers by default.

## 7. Retrieval and context assembly

1. Derive task identity, project ID, relevant file areas, language, task category, and current base commit.
2. Filter by authorized scope, status, sensitivity, expiry, and applicable repository revision.
3. Retrieve candidates with SQLite FTS5 (BM25) and exact references/tags.
4. Rank using relevance, verified provenance, recency, and task applicability; do not interpret a numerical confidence as scientific certainty.
5. Deduplicate and collapse superseded records.
6. Select under a strict configurable token budget; truncate only at record boundaries.
7. Return a structured bundle with memory IDs, source references, status, and retrieval reason.
8. Label all retrieved content as **untrusted task data** in provider prompts. Never allow it to override user instructions, project policy, or approvals.

Suggested initial default: at most 8 records and approximately 1,200 tokens per worker; measure and tune. Missing or failed retrieval must not prevent a worker from executing a task unless the task explicitly requires memory.

## 8. Ingestion, verification and consolidation

**Before task:** retrieve only relevant context, and include provenance in the prompt.

**During task:** allow worker proposals to be queued with job/task/agent IDs, not direct edits to the database.

**After task:** the orchestrator collects completion artifacts, test results, reviewer decisions, commit hashes, and failure classifications. Deterministic extractors capture structured outcomes without extra LLM calls. Optional model-assisted summarization is a separately authorized, metered feature.

**Promotion policy:** a passing process exit code or an agent's self-report alone never establishes a verified memory. Attach the actual test/commit/review evidence; distinguish `reported` from `verified`. Promotion does not authorize merging or deployment.

**Consolidation:** exact duplicate matching in M1; optional semantic duplicate suggestions in M2. Conflicting facts remain visible with source versions until resolved. Use append-only revision history, explicit supersession, and reversible retraction rather than silent overwrites.

**Aging:** time-sensitive environment observations may expire; architectural decisions remain until explicitly superseded. Offer inspect/export/delete with privacy-preserving deletion policy, including FTS index cleanup, backups, and WAL checkpoint handling.

## 9. Security and reliability

- Redact credentials, tokens, personal data, and secrets before ingestion; reject known sensitive paths such as `.env` and key files.
- Prevent prompt injection: retrieved content is evidence, not commands; never execute retrieved shell instructions automatically.
- Enforce project isolation and least-privilege actor roles.
- Use parameterized SQL, size limits, UTF-8 validation, and canonical path checks.
- Handle simultaneous writers, busy databases, power interruption, schema migration failures, and corrupted indexes.
- Do not expose memory files to Git or publish them in repository commits.
- Support encrypted backups as an optional later enhancement; SQLite is not encrypted by default.
- Keep `main`, remote pushes, destructive operations, and live provider calls behind existing approval gates.
- Document that worktrees are edit isolation, not a security sandbox.

## 10. Implementation milestones

### M1 — Durable foundation (first implementation PR)

- Add `src/multi_agent_ai/memory/{schema,store,search,policy,context}.py` and package exports.
- Create a dedicated SQLite database and versioned migration mechanism.
- Implement validated records, scopes, provenance, revision history, optimistic concurrency, and audit events.
- Add FTS5 search with project/status filters.
- Add CLI init/add/search/show/promote/retract/doctor.
- Add deterministic tests for restart persistence, concurrent writes, isolation, source linkage, search updates, retractions, schema upgrades, corruption handling, and Windows file locking.
- Update README and implementation-status documentation.
- No provider invocation, embeddings, or background model calls.

**Acceptance:** all new and existing pytest tests pass; restart retains records; concurrent writes do not lose updates; unauthorized cross-project search returns no data; stale revisions are rejected; retracted records do not appear in normal search; `memory doctor` is read-only.

### M2 — Agent integration

- Integrate `MemoryContext.build` with deterministic managed worker prompt construction.
- Enforce token budgets and provenance labels.
- Capture job/attempt outcomes idempotently from verified orchestrator events.
- Add proposal review and safe consolidation.
- Optionally add local embedding adapters behind an explicit feature flag, with no required network service.

**Acceptance:** memory-enabled and memory-disabled runs produce comparable audit records; memory failures degrade gracefully; no retrieved text bypasses tool/approval policy; injected context remains within budget.

### M3 — Measured learning and routing

- Connect verified task outcomes to the planned performance ledger.
- Introduce evidence-based agent routing using observed success, retries, latency, and measured/estimated cost (kept distinct).
- Benchmark single-agent, multi-agent without memory, and multi-agent with memory on repeatable task suites.
- Report retrieval precision, task success, token overhead, time-to-completion, false-memory rate, and cross-project leakage tests.

**Acceptance:** demonstrate measured benefits (or document lack of improvement); preserve a feature flag to disable memory-assisted routing.

## 11. Proposed repository changes

```text
src/multi_agent_ai/memory/
  __init__.py
  schema.py
  store.py
  search.py
  policy.py
  context.py
  extractor.py          # M2
  consolidation.py      # M2
  embeddings.py         # optional M2
tests/
  test_memory_store.py
  test_memory_search.py
  test_memory_policy.py
  test_memory_context.py
docs/
  AI-persistent-memory.md
```

Prefer minimal changes to existing `cli.py`, `state.py`, and worker prompt builder; maintain backward compatibility with existing coordination files. Keep the memory store project-neutral, and do not hard-code EPESI or other application-specific terms.

## 12. Open design decisions

1. Whether to retain a dedicated `memory.sqlite3` long-term or merge into a future canonical database (dedicated is recommended initially).
2. Which actors can promote records to `verified` and which evidence classes qualify.
3. Whether project-scoped memories can be selectively published into a user-approved global knowledge base.
4. Whether to support encrypted-at-rest storage and a secure cross-machine synchronization service later.
5. Whether model-assisted extraction is worth its quota cost relative to deterministic outcome capture.

## 13. Delivery and approval workflow

Create implementation work on a separate branch/worktree with a single owner per file. Use fake providers and local tests first. A different agent should review exact commits and rerun verification in a fresh environment. Do not merge or push to `main` without explicit user approval. Follow `AGENTS.md`, `docs/agent-rules.md`, and `docs/protocol/` if this proposal conflicts with established rules.

**Recommended next step:** approve M1 as an implementation task, assign a coordinator, and build the persistence and CLI foundation before attempting automated agent learning.

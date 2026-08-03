# Azure Migration

Goal: scale the platform to roughly **2,000 users** on Azure Container Apps.

This document is an ordered migration process, not a redesign. It assumes the
[architecture](architecture.md) is correct and focuses on the execution model,
storage, and infrastructure changes that make horizontal scale-out possible.
Cross-reference [opportunities](opportunities.md) for the known defects and
debt this migration either fixes or touches.

## 1. Current architecture snapshot

- **API/UI**: FastAPI. All routes are synchronous `def` endpoints
  (`routers/api.py`, `ui.py`), so FastAPI runs them on the default worker
  thread pool (~40 threads per process).
- **Database**: PostgreSQL via sync SQLAlchemy/SQLModel, `pgvector` for
  evidence search (`db_ops.create_pgvector_extension`).
- **Auth**: WorkOS AuthKit OAuth + signed-cookie `SessionMiddleware`
  (`auth_service.py`) — stateless, no server-side session store.
- **Storage**: `LocalObjectStore` / `LocalArtifactStore` on the local
  filesystem (`dependencies.get_artifact_store`, `artifact_store.py`,
  `file_ops.py`). Uploads, target schemas, generated artifacts, and execution
  logs live under `data/`.
- **Execution**: `execute_approved_mapping` (`workflow.py`) runs the generated
  standalone `pipeline.py` in-process via `subprocess.run`
  (`codegen.execute_generated_pipeline`).
- **LLM calls**: inline synchronous OpenAI calls in the request path —
  `mapping.call_mapping_llm`, `mapping.call_codegen_llm`, `db_ops.get_embedding`.
- **Static assets**: served by the FastAPI app (`app.py` mounts `/static`).

## 2. Why the current design does not scale out

### Blocker 1 — inline blocking work in the request thread pool

Long-running work happens inside sync endpoints:

- A pipeline run is a `subprocess.run` of the generated Polars script
  (`codegen.py` `execute_generated_pipeline`), which can take 30-120s.
- Each run is preceded/followed by blocking OpenAI calls (mapping proposal,
  codegen retry loop, evidence embeddings).

One run holds a thread-pool worker for its entire duration. With ~40 workers
per replica, a modest burst of "execute pipeline" / "retry" / "chat" requests
saturates the pool and **stalls every endpoint on that replica**, including
fast ones. This is the primary concurrency ceiling.

### Blocker 2 — local filesystem object store

Artifacts live on the replica's disk. With more than one replica, an upload
written to one instance may be requested from another and return 404. Scale-out
is unsafe until the store is shared.

### Secondary issues

- Static assets served by the app instead of a CDN/Blob.
- SQLAlchemy engine uses default pool sizing (`create_engine(url)`).
- Uploads are read fully into memory (`api.upload_raw_file` reads
  `file.file.read()`); large files stress request bodies and RAM.
- `AUTH_BYPASS_LOCAL` must be disabled in production.
- LLM calls have no per-user rate limiting or queue; OpenAI rate limits become
  the bottleneck under load.

## 3. Agreed design decisions

1. **Async-ify the OpenAI-dependent request path.** The LLM layer
   (`call_mapping_llm`, `call_codegen_llm`, `get_embedding`) becomes `async def`
   using `AsyncOpenAI`; the endpoints that drive it (retry, chat, confirm,
   propose, evidence search, embedding ingestion) become `async def`. LLM waits
   then run on the event loop and stop consuming thread-pool workers.
2. **Async discipline.** LLM I/O is `await`ed directly on the event loop. Sync
   SQLAlchemy and subprocess work stay synchronous and are wrapped in
   `asyncio.to_thread` when heavy. **No `asyncio.run` in server code.**
   FastAPI `async def` endpoints must never call blocking code directly.
3. **Execution offloaded to a unit of compute per run.** Pipeline execution is
   removed from the API replica entirely. Each run maps to one isolated
   execution — **Container Apps Jobs** (event-driven) is the recommended
   target; **Durable Functions** is the alternative (see decisions log).
4. **Object store becomes Azure Blob.** Inputs, generated artifacts, and output
   CSVs all move through Blob behind the existing `ObjectStore`/`ArtifactStore`
   seam, so no workflow or route code changes.

## 4. Target Azure architecture

```
                  ┌──────────────────── Azure Container Apps ────────────────────┐
 users ─HTMX/JSON─▶ API replicas (FastAPI, async endpoints)                      │
                  │   - HTTP-concurrency scale rule                              │
                  │   - publishes jobs, polls ExecutionRun.status                │
                  └───────────────┬──────────────────────────────┬───────────────┘
                                  │ publish                      │ read/write
                                  ▼                              ▼
                    Azure Queue Storage ────▶ Container Apps Jobs  Azure Blob Storage
                    (job messages: spec_id,  (event-driven; one    (raw files,
                     run_id, artifact refs)   instance per run:     schemas, artifacts,
                                              pull inputs, run      results, logs)
                                              pipeline.py, push
                                              outputs, record)
                                              │
                                              ▼
                                    Azure Database for PostgreSQL Flexible Server
                                    (pgvector enabled, index the embedding column)
```

- **API**: Container Apps app, 1-3 replicas, HTTP-concurrency scaling.
- **Execution**: event-driven Container Apps Jobs; the job image is small
  (`python` + `polars` + `openpyxl` + the executor code) because the generated
  `pipeline.py` is standalone — it imports only stdlib, `polars`, and
  `openpyxl` (`codegen.py` imports block) and reads `mapping.json` plus
  `--source-folder`/`--output-folder`.
- **Queue**: Azure Queue Storage (Service Bus if ordering/dead-lettering
  matters).
- **Database**: Azure Database for PostgreSQL Flexible Server with `pgvector`.
- **Storage**: Azure Blob Storage behind the object-store seam.
- **Static**: Azure CDN/Front Door or Blob static-site hosting.
- **Secrets**: Azure Key Vault (DB connection string, OpenAI key, WorkOS keys,
  session secret).
- **Auth**: WorkOS AuthKit unchanged; `AUTH_BYPASS_LOCAL=false`.

## 5. Migration phases

Each phase is independently shippable and has exit criteria. Code refs are to
the current `dev` branch.

### Phase 0 — green baseline

Get the default check suite passing before touching architecture.

- Fix the failing unit test: `test_parse_llm_mapping_response_builds_proposed_mappings`
  asserted a `confidence` field that local `dev` no longer has; the assertion
  and fixture key were removed (`tests/test_mapping.py`).
- Fix lint: remove unused `table_index_text` in `mapping.py`
  (`build_codegen_retry_prompt`; the prompt uses `_build_column_detail_text`).
- Run `uv run ruff format --check backend/src/ tests/` and reformat the 14
  drifted files, or record them as accepted debt.

Exit criteria: `uv run ruff check backend/src/ tests/` clean; `uv run pytest`
green (DB-dependent tests skipped locally); format drift decided.

### Phase 1 — async LLM layer

Remove LLM waits from the thread pool.

- `backend/src/mapping.py`
  - `call_mapping_llm` and `call_codegen_llm` → `async def`, `AsyncOpenAI`,
    `await client.chat.completions.create(...)`.
  - `_gather_targeted_evidence` → `async def` (calls async search).
  - `propose_mapping_spec` (contains the `call_mapping_llm` call) → `async def`.
- `backend/src/db_ops.py`
  - `get_embedding` → `async def`, `AsyncOpenAI`.
  - `search_evidence_by_text` → `async def`.
  - Evidence-extraction loop (ingestion) → async.
- `backend/src/feedback.py` — `propose_refinements`, `store_feedback` → `async def`.
- `backend/src/workflow.py` — `refine_mapping`, `propose_mapping`,
  `ingest_and_propose`, `approve_mapping_and_execute`, `retry_pipeline_and_execute`,
  `apply_refinements_and_reexecute` → `async def`. `execute_approved_mapping`
  stays sync and is called via `await asyncio.to_thread(...)`. `apply_refinements`
  stays sync (fast file writes) and is called directly.
- `backend/src/ui.py` — convert to `async def` + `await`: `mapping_chat`,
  `mapping_chat_apply`, `results_chat_apply`, `mapping_confirm`, `mapping_retry`,
  the upload-wizard propose route.
- `backend/src/routers/api.py` — convert to `async def`: `search_evidence_endpoint`,
  `propose_mapping_spec_endpoint`, `parse_raw_file`, `ingest_client_folder_endpoint`,
  `execute_pipeline`.
- Callers: `run_example.py` wraps `propose_mapping_spec` in `asyncio.run`;
  `tests/conftest.py` fakes for `get_embedding` / `call_mapping_llm` become
  `async def`; TestClient tests pass unchanged (anyio runs async routes).

Discipline: LLM I/O is the only thing awaited directly; heavy sync bodies go in
`asyncio.to_thread`; no `asyncio.run` in server code; sync SQLAlchemy in async
endpoints is accepted (fast queries) and flagged for a future async-SQLAlchemy
migration.

Exit criteria: `uv run pytest`, `ruff check`, `ruff format --check`, and `mypy`
(all no-new-errors); manual HTMX smoke of chat → apply, confirm, retry, and the
upload wizard with `AUTH_BYPASS_LOCAL=true`.

### Phase 2 — Azure Blob object store

Make scale-out safe by sharing artifacts.

- Implement an `AzureBlobObjectStore` behind the `ObjectStore` ABC
  (`file_ops.py`) and `AzureArtifactStore` behind `ArtifactStore`
  (`artifact_store.py`).
- Wire the factory in `dependencies.py` from config
  (`AZURE_STORAGE_CONNECTION_STRING` / container names) while keeping
  `LocalArtifactStore` as the default for local dev.
- Add the settings to `config.py`.

Exit criteria: upload → ingest → generate → execute round-trips fully through
Blob on a two-replica dev deployment; local dev still works with the local
store.

### Phase 3 — durable execution

Move pipeline runs off the API replica and into the job system.

- New worker entrypoint (a container `execute_job`): given a job message
  (`spec_id`, `run_id`), pull inputs + generated artifacts from Blob, run
  `pipeline.py`, push result CSVs back, then do the post-execution steps
  currently inside `execute_approved_mapping` (`run_validation_tests`,
  `record_staging_metadata`) and set `ExecutionRun` status.
- API endpoints `mapping_confirm`, `mapping_retry`,
  `apply_refinements_and_reexecute`, and `execute_pipeline` publish a queue
  message and return `run_id` immediately instead of running inline.
- Use the existing `ExecutionRun` model (`status` enum
  `PENDING/RUNNING/SUCCESS/PARTIAL/FAILED`, `logs`, `started_at`, `finished_at`):
  add status transitions and capture `stdout`/`stderr` in `logs`.
- Results page: add a "running" state that polls `get_execution_run` (HTMX)
  instead of redirecting only after completion.
- Remove the Phase 1 `asyncio.to_thread` stopgaps around execution.

Exit criteria: confirm/retry/apply return immediately; a run transitions
PENDING → RUNNING → SUCCESS/FAILED; results page shows progress and final
outputs; failed runs surface the pipeline stderr.

### Phase 4 — Azure infrastructure

Provision and configure the target environment (resource list; full IaC out of
scope for this doc).

- Resources: Container Apps environment (app + jobs), Azure Queue Storage,
  Azure Database for PostgreSQL Flexible Server (pgvector), Azure Blob Storage
  account, Key Vault, Application Insights, optional CDN/Front Door.
- Config: disable `AUTH_BYPASS_LOCAL`; move secrets to Key Vault; set
  `DATABASE_URL`, OpenAI keys, and storage connection strings via env.
- Database: raise SQLAlchemy `pool_size`/`max_overflow` (or add PgBouncer for
  many replicas); index the `pgvector` embedding column.
- Static assets: serve from Blob/CDN instead of the app.
- Ingress: check/raise the Container Apps request-body limit to fit large
  uploads; stream uploads to Blob instead of reading into memory.
- Scaling rules: HTTP-concurrency for the API app; queue-based for jobs
  (with concurrency cap per run of the polars image).

Exit criteria: clean deployment from a fresh `az`/Bicep run; end-to-end flow
against managed resources; `make check` green in CI.

### Phase 5 — load validation and hardening

- Load test at 2,000 users (k6 or similar): steady-state browsing plus bursts
  of chat/propose/execute.
- Tune replica counts, concurrency, and job parallelism; verify no thread-pool
  or event-loop saturation and no artifact 404s across replicas.
- Add per-user OpenAI rate-limit guardrails (backoff/queue) so LLM provider
  limits cannot stall the event loop or API.
- Enable Application Insights and alert on run failures, queue depth, and p95
  latencies.

Exit criteria: p95 under target at 2,000 concurrent users; executions complete
durably; alerts wired.

## 6. Effort estimate

Rough, single-senior-engineer sizing; phases can overlap.

| Phase | Focus | Effort |
| --- | --- | --- |
| 0 | Green baseline (test + lint) | already largely done |
| 1 | Async LLM layer | ~1 day |
| 2 | Blob object store | ~1 day |
| 3 | Durable execution (queue + worker + status) | ~2 days |
| 4 | Azure infrastructure | ~2 days |
| 5 | Load validation and hardening | ~1-2 days |
| **Total** | | **~1 week** |

## 7. Decisions log and open questions

Decisions:

- **Async before queue**: the async LLM layer is independent of durability and
  pays off immediately, so it ships first. The `asyncio.to_thread` execution
  stopgap from Phase 1 is explicitly throwaway, replaced by Phase 3 queueing.
- **Container Apps Jobs over Durable Functions**: each run is a long CPU-bound
  polars script; a job instance per queue event has no runtime limit and no
  "subprocess inside a Functions host" awkwardness. Durable Functions remains
  viable if a turnkey orchestrator/retry story is preferred.
- **Sync SQLAlchemy in async endpoints**: accepted for now (fast queries);
  an async-SQLAlchemy migration is out of scope and flagged in
  [opportunities](opportunities.md).
- **Tiny execution image**: the generated `pipeline.py` is standalone
  (stdlib + `polars` + `openpyxl` only), so the job container does not carry
  the full backend.

Open questions:

- Managed PostgreSQL vs PgBouncer for connection pooling at multi-replica scale.
- Multi-tenant isolation approach at 2,000 users (per-client DB vs shared).
- Whether chat/propose LLM calls also need a durable queue once provider rate
  limits bind.
- Static-asset strategy: Blob static hosting vs CDN/Front Door.

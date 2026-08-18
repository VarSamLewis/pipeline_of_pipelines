# Architecture

> On-premises/hosting decision? See [Azure migration](azure_migration.md) for
> the process of moving this platform to Azure Container Apps.

Pipeline of Pipelines is an auditable, LLM-assisted data-transformation
platform. A client uploads heterogeneous raw files (spreadsheets, PDFs,
emails, text documents) and a target schema; the system extracts evidence,
proposes a source-to-target mapping, and generates a standalone Polars
pipeline. Every step is **human-gated** before it progresses.

```
 raw files ──▶ ingest ──▶ evidence + profiles ──▶ LLM mapping proposal ──▶ human approve
                                                                              │
                                                                              ▼
                                   results.csv ◀── validate & record ◀─── run pipeline.py ◀─── generate artifacts
                                       │                                              ▲
                                       ▼                                              │
                                  human confirm                              human review code
```

## Layered responsibilities

The backend is split so that HTTP code only validates input and translates
responses; all real work lives in workflow services.

| Layer | Modules | Responsibility |
| --- | --- | --- |
| Composition | `app.py`, `config.py`, `dependencies.py` | FastAPI app, lifespan, settings, dependency factories |
| HTTP/UI | `routers/api.py`, `ui.py` | JSON API + HTMX UI routes, auth wiring, template rendering |
| Workflow | `workflow.py` | Canonical human-gated orchestration; the only owner of transitions |
| Domain services | `mapping.py`, `feedback.py`, `codegen.py`, `pipeline.py` | LLM prompting/parsing, refinement, artifact + script generation, validation |
| Parsing | `parser.py`, `file_ops.py` | File-type detection, tabular discovery/profiling, text/email/PDF extraction |
| Persistence | `db_ops.py`, `repositories/` | SQLModel CRUD, evidence search, audit/lineage; client & execution run ops |
| Storage | `artifact_store.py`, `file_ops.ObjectStore` | Immutable raw-file object store + generated-artifact store |
| Models | `models.py` | Pydantic schemas + SQLModel tables + enums |
| Auth | `auth_service.py` | WorkOS AuthKit, encrypted sessions, role checks, local dev bypass |
| Templates | `backend/templates/` | Jinja2 + HTMX pages (`upload`, `mapping`, `results`) |

## Module map

- **`app.py`** — builds the FastAPI app, adds `SessionMiddleware`, mounts
  `/static`, and registers the UI and API routers. The lifespan calls
  `create_tables()` on startup (idempotent, no migrations yet).
- **`config.py`** — frozen `Settings` dataclass read from env once via
  `get_settings()` (lru_cache). Canonical dev paths (`data/`, `static/`,
  `templates/`) are derived from the project root. `AUTH_BYPASS_LOCAL` skips
  WorkOS for local dev and tests.
- **`dependencies.py`** — composition boundary for local adapters:
  `get_artifact_store()` / `get_object_store()` return `LocalArtifactStore`.
  An Azure/S3 backend can swap in without touching workflow or route code.
- **`workflow.py`** — the orchestrator. Owns `ingest_and_propose`,
  `approve_mapping`, `generate_artifacts` / `create_output_folder`,
  `execute_approved_mapping`, `approve_result`/`reject_result`,
  `retry_pipeline_and_execute`, and the feedback loop (`refine_mapping`,
  `refine_from_results`, `apply_refinements`, `apply_refinements_and_reexecute`).
- **`mapping.py`** — LLM-side of mapping: builds composite-key-grounded
  prompts (`filename::sheet::column`), calls an OpenAI-compatible chat model,
  parses JSON into `ProposedMapping`s, normalizes common Polars mistakes, and
  resolves/validates composite keys back to the source catalog.
- **`feedback.py`** — chat-driven refinement: proposes column-level diffs from
  user feedback (optionally with failed-validation context), and stores each
  feedback message as durable evidence (`user_feedback` chunk).
- **`codegen.py`** — LLM-assisted generation of `pipeline.py` (a standalone,
  self-contained Polars script) and `mapping.json`. A deterministic draft is
  produced first, then an LLM rewrites/fixes the transformation logic while
  preserving the harness. Also handles subprocess execution, dtype enforcement,
  and retry codegen with error context.
- **`pipeline.py`** — output-side: builds/executes validation tests, computes
  quality profiles, and records `ExecutionRun`, `ValidationResult`,
  `StagingTable`, `StagingColumn` metadata. Note the module-level distinction:
  this is validation/recording, the generated pipeline is what executes.
- **`parser.py`** — pure parsers/profilers. CSV (encoding/delimiter/header
  discovery, ragged-row detection), XLSX (multi-table region discovery across
  blank-separated runs, merged-cell detection), PDF, EML, TXT/MD/DOCX. Produces
  `SourceCatalog`/`SourceTable`/`SourceColumn` with deterministic ids.
- **`db_ops.py`** — SQLModel CRUD, embedding + pgvector evidence search, audit
  log, lineage, folder ingestion, parsing orchestration.
- **`repositories/clients.py`**, **`repositories/executions.py`** — thin
  persistence helpers for client/batch and run approval/rejection transitions.
- **`artifact_store.py`** — `ArtifactStore` boundary + `LocalArtifactStore`
  adapter. Raw uploads, target schemas, generated artifacts, and execution
  logs live under `data/` on disk.
- **`auth_service.py`** — WorkOS AuthKit OAuth, role hierarchy
  (creator < reviewer < approver < admin), encrypted cookie sessions, and the
  `require_auth` / `require_role` FastAPI dependencies.

## The human-gated workflow

The UI (`ui.py`) exposes a linear three-step flow:

1. **Upload** (`/upload`) — pick an existing client or create one, upload one
   or more source files plus a `target_schema.json`. `ingest_and_propose`
   stores files immutably, parses them (profiles + evidence chunks with
   embeddings), then asks the LLM to propose a mapping.
2. **Review mapping** (`/mapping/{spec_id}`) — the LLM proposal is rendered for
   review with target-column metadata and parse warnings. Human can **Confirm**
   (approve → codegen → execute), **Reject**, or use the **chat** sidebar to ask
   for refinements.
3. **Review results** (`/results/{run_id}`) — paginated CSV preview plus the
   generated `pipeline.py`. Human can **Confirm** (publish the run), **Reject**
   (return to mapping), or chat about validation failures to re-execute.

The feedback loop: a chat message is turned into structured proposals
(field-level changes like `polars_expression`, `filter_expression`,
`lookup_value`), rendered as a diff with an **Apply** button, and applied to
`MappingColumn`s before regenerating artifacts.

### API lifecycle (also exposed as endpoints)

The JSON API (`routers/api.py`) mirrors the same gates and documents the
intended human review sequence in its module docstring:

1. Is it mapped correctly? → `POST /mapping-specs/{id}/propose` + `/approve`
2. Does the code make sense? → `POST /mapping-specs/{id}/generate`,
   `GET /output-folders/{id}/pipeline.py`
3. Do the results match? → `POST /mapping-specs/{id}/execute`,
   `GET /output-folders/{id}/results.csv`, then
   `POST /execution-runs/{id}/approve`

## Execution model

`execute_generated_pipeline` (in `codegen.py`) runs the generated script in a
subprocess with `--source-folder` (a temp dir populated with the immutable raw
files, named `<raw_file_id>_<original_filename>`) and `--output-folder`. The
generated script resolves source files via `mapping.json`'s catalog (original
filename or `raw_file_id_*` glob), verifies content hash, reads the correct
CSV region / XLSX sheet+range, applies per-table transformations, enforces
target dtypes, and writes one CSV per target table. Back in the app,
`execute_approved_mapping` runs validation tests against the outputs, persists
the run, validation results, and staging lineage, and writes a durable
`execution-logs/{run_id}.json`.

## Data model highlights

- **`Client`** → **`IngestionBatch`** → **`RawFile`** (immutable, SHA-256,
  deterministic storage key) → **`SpreadsheetProfile`** / **`ExtractedEvidence`**
  (pgvector `Vector(1536)` embedding).
- **`MappingSpec`** (versioned, status lifecycle) → **`MappingColumn`** rows
  carrying the full transformation contract (source refs, Polars expressions,
  tests, evidence/rule citations).
- **`ExecutionRun`** → **`ValidationResult`**, **`StagingTable`** →
  **`StagingColumn`** (lineage links back to `MappingColumn`).
- **`AuditLog`** (append-only) and **`LineageEdge`** (generic provenance graph).
- **`User`** caches WorkOS identity/role; WorkOS metadata remains the source of
  truth.

## Auth model

Roles: `creator` < `reviewer` < `approver` < `admin`. `require_role(*roles)`
compares hierarchy levels. HTMX requests get an `HX-Redirect` header on 401 so
the browser lands on `/login`. With `AUTH_BYPASS_LOCAL=true`, a synthetic admin
user is used and everything is open.

## UI / HTMX notes

- Pages are Jinja2 templates under `backend/templates/` (base + `upload`,
  `mapping`, `results`), with fragments in `partials/` (chat sidebar, diff,
  CSV table, code view, errors).
- HTMX drives partial swaps; server responses set `HX-Redirect` for navigation.
- `chat_diff.html` renders each proposed refinement as a real HTMX `<form>`
  posting to `/mapping/{spec_id}/chat/apply` or `/results/{run_id}/chat/apply`
  with a hidden `changes_json` field (single-quoted attribute, `tojson`-safe).

## Storage layout (local adapter)

```
data/
  object-store/        immutable raw files:  <client>/<batch>/<sha16>_<name>
  target-schemas/      <client>/target_schema.json
  output-folders/      <spec_id>/pipeline.py, mapping.json, <table>.csv, results.csv
  execution-logs/      <run_id>.json
```

## Configuration knobs

See `config.py`. Notable env vars: `DATABASE_URL`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `MAPPING_MODEL` (default `gpt-4o-mini`), `CODEGEN_MODEL`
(default `gpt-4o-mini`), `EMBEDDING_MODEL`
(default `text-embedding-3-small`), `AUTH_BYPASS_LOCAL`, `SESSION_SECRET_KEY`,
`SESSION_MAX_AGE`, plus WorkOS vars.

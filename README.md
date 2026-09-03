# Pipeline of Pipelines

Auditable, LLM-assisted data-transformation platform. Clients upload
heterogeneous raw files (spreadsheets, PDFs, emails, text documents) plus a
target schema. The platform stores the files immutably, extracts evidence,
builds versioned human-approved mapping specifications, generates a standalone
Polars transformation pipeline, executes it, validates the results, and records
full column-level lineage. Every step is **human-gated**.

Documentation: [Architecture](docs/architecture.md) ·
[Opportunities for improvement](docs/opportunities.md) ·
[Azure migration](docs/azure_migration.md) ·
[Azure setup guide](docs/azure_setup_guide.md)

## Features

- **Linear three-step UI** (HTMX + Jinja2): upload → review mapping → review results.
- **LLM mapping proposals** grounded in composite source keys
  (`filename::sheet::column`) and pgvector evidence search.
- **Chat-driven refinement**: give feedback on the mapping or results pages and
  apply LLM-proposed column-level changes, then re-execute.
- **Standalone generated `pipeline.py`**: a self-contained Polars script plus a
  machine-readable `mapping.json`.
- **Validation and lineage**: per-test validation results, staging metadata,
  append-only audit log, and a generic provenance graph.

## Tech stack

- Python 3.13+
- FastAPI + Pydantic v2 + SQLModel
- PostgreSQL 16+ with `pgvector`
- Polars for target transformations
- OpenAI embeddings + OpenAI-compatible chat LLM for mapping proposals
- Microsoft Entra ID (OAuth) for authentication and role-based authorization
- HTMX + Jinja2 for the UI
- Local filesystem object store for dev; Azure Blob Storage in the hosted
  (App Service) deployment (`infra/`)

## Project layout

```
backend/
  src/
    app.py                FastAPI composition, middleware, router registration
    config.py             Environment-based settings + canonical dev paths
    dependencies.py       Dependency factories (object store / artifact store)
    ui.py                 HTMX UI routes (upload / mapping / results + chat)
    routers/
      api.py              JSON API endpoints for the full lifecycle
    workflow.py           Canonical human-gated orchestration
    mapping.py            LLM prompt building, parsing, key resolution, validation
    feedback.py           Chat refinement proposals + feedback storage
    codegen.py            Standalone Polars script + mapping.json generation & execution
    pipeline.py           Output validation, execution recording, staging metadata
    mapping_specs.py      Canonical mapping-spec query representation
    parser.py             Parsers/profilers: CSV, XLSX, PDF, EML, TXT/MD/DOCX
    db_ops.py             Database CRUD, evidence search, audit, lineage
    repositories/
      clients.py          Client / ingestion-batch persistence
      executions.py       Execution-run approval/rejection transitions
    artifact_store.py     Durable artifact storage boundary + local adapter
    file_ops.py           Object-store abstraction, file-type detection, hashing
    models.py             Pydantic schemas + SQLModel tables + enums
    auth_service.py       Microsoft Entra ID OAuth, sessions, role checks, local bypass
    templates/            Jinja2 pages + HTMX partials
    static/               Static assets
  tests/                  pytest unit and integration tests
  examples/               Example client fixtures
  docs/                   Architecture and improvement-opportunity docs
```

## Installation

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency management.
From the project root:

```bash
uv sync --extra dev
```

(Equivalently: `uv pip install -e ".[dev]"`.) This installs the runtime
dependencies plus `pytest`, `ruff`, and `mypy`.

## Database

Start PostgreSQL + pgvector with Docker Compose:

```bash
docker compose up -d postgres
```

The default connection URL is:

```
postgresql+psycopg://postgres:postgres@localhost:5432/pipeline
```

Override it with `DATABASE_URL`. The tests use
`postgresql+psycopg://postgres:postgres@localhost:5432/pipeline_test` and skip
automatically when Postgres is unreachable.

## Running the server

An OpenAI API key is required for embeddings and mapping proposals. For local
development, auth is bypassed so you can use the UI without Entra ID:

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export AUTH_BYPASS_LOCAL="true"
uv run uvicorn app:app --app-dir backend/src --reload
```

The app is then available at `http://127.0.0.1:8000` (root redirects to the
upload page). Visit `/docs` for the interactive OpenAPI UI.

### Using the UI

1. Open `http://127.0.0.1:8000/upload`.
2. Select an existing client or create one, upload one or more source files
   (`.csv`, `.xlsx`, `.xls`, `.pdf`, `.txt`, `.eml`, `.msg`, `.docx`) and a
   `target_schema.json`, then click **Run mapping**.
3. Review the LLM-proposed mapping on the mapping page. Use **Confirm** to
   generate and execute the pipeline, or **Reject** / the chat sidebar to
   adjust.
4. Review the generated results CSV and `pipeline.py`. **Confirm** to publish,
   **Reject** to return to mapping, or chat to fix validation failures and
   re-execute.

### Using the JSON API

The API exposes the same gates; see `routers/api.py` for the full lifecycle and
the OpenAPI docs at `/docs`. Typical sequence:

```bash
curl -X POST http://127.0.0.1:8000/clients \
  -H "Content-Type: application/json" \
  -d '{"name": "Acme Corp", "code": "acme"}'

curl -X POST "http://127.0.0.1:8000/clients/acme/target-schema" \
  -F "schema_file=@examples/complex_client/target_schema.json"

curl -X POST "http://127.0.0.1:8000/clients/acme/ingest-folder" \
  -F "folder_path=examples/complex_client"

curl -X POST "http://127.0.0.1:8000/clients/acme/mapping-specs" \
  -H "Content-Type: application/json" \
  -d '{"source_raw_file_ids": ["<ids>"], "target_schema": {...}}'

curl -X POST "http://127.0.0.1:8000/mapping-specs/<spec-id>/propose"
curl -X POST "http://127.0.0.1:8000/mapping-specs/<spec-id>/approve" \
  -H "Content-Type: application/json" \
  -d '{"reviewer": "data-guardian"}'

curl -X POST "http://127.0.0.1:8000/mapping-specs/<spec-id>/output-folder"
```

This produces exactly three deliverables in the output folder:

- `pipeline.py` — standalone Polars transformation script
- `mapping.json` — human- and machine-readable mapping spec
- `results.csv` — transformed output

Retrieve them at:

```bash
curl http://127.0.0.1:8000/output-folders/<spec-id>/pipeline.py
curl http://127.0.0.1:8000/output-folders/<spec-id>/mapping.json
curl http://127.0.0.1:8000/output-folders/<spec-id>/results.csv
```

## Example

`examples/complex_client/` contains a single rich, multi-source fixture:

- `master_data.xlsx` — multi-sheet workbook with customers, orders, and products
- 4 PDFs — onboarding guide, region reference, revenue policy, product catalogue
- 5 emails — data quality, order validation, region mapping, known issues,
  reporting requirements
- 3 text files — business glossary, provenance notes, contacts
- `target_schema.json` — target schema for `customers` and `orders` tables

The LLM mapper reads the source profiles and the most relevant evidence from
pgvector to propose mappings. Computed fields such as `total_revenue` and
`line_total` are left for human review because they require expressions.

Regenerate the fixture files with:

```bash
python examples/complex_client/generate_fixture.py
```

## Testing

Use `uv run` (all commands also work via `python -m`):

```bash
docker compose up -d postgres
export OPENAI_API_KEY="sk-..."
uv run pytest
```

Integration tests need the PostgreSQL container running; unit tests that do not
touch the database run without Postgres, and integration tests are skipped
automatically if Postgres is unreachable. The `mock_openai` fixture stubs
embeddings and chat completions for deterministic LLM-related tests.

Run the full check suite (lint, format, typecheck, tests):

```bash
make check
```

Or individually:

```bash
make lint        # uv run ruff check backend/src/ tests/
make fmt         # uv run ruff format --check backend/src/ tests/
make typecheck   # uv run mypy backend/src/
make test        # uv run pytest
```

## Hosting (Azure deployment)

The app is self-hosted on Azure, provisioned entirely with Terraform in
[`infra/`](infra/): a single FastAPI container on **Azure App Service**
(Linux), pulling from Azure Container Registry, with a **private** PostgreSQL
Flexible Server, Azure Blob Storage, Azure OpenAI, Key Vault, and Microsoft
Entra ID auth.

```bash
cd infra/environments/dev
terraform init -input=false -backend-config=backend.hcl
terraform plan  -var-file=params.tfvars -var-file=secrets.tfvars
terraform apply -var-file=params.tfvars -var-file=secrets.tfvars
```

See the **[Azure setup guide](docs/azure_setup_guide.md)** for the full walkthrough:
resource layout, per-environment config, Entra roles/groups, secret access,
troubleshooting, and adding staging/prod.

## Configuration

Key environment variables (full list in `backend/src/config.py`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/pipeline` | Postgres connection |
| `OPENAI_API_KEY` | — | Required for embeddings and mapping proposals |
| `OPENAI_BASE_URL` | — | OpenAI-compatible base URL |
| `MAPPING_MODEL` | `gpt-4o-mini` | Chat model for mapping proposals |
| `CODEGEN_MODEL` | `gpt-4o-mini` | Chat model for pipeline code generation |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `AUTH_BYPASS_LOCAL` | unset | When `true`, bypass Entra ID and use a synthetic admin |
| `SESSION_SECRET_KEY` | random | Session cookie signing key (set in production) |
| `ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` / `ENTRA_CLIENT_SECRET` | — | Entra ID OAuth credentials for auth (see [Azure setup guide](docs/azure_setup_guide.md)); not needed with `AUTH_BYPASS_LOCAL` |

There is no heuristic fallback: both ingestion (embeddings) and mapping
proposals require an LLM API key.

## Design principles

1. **Immutability** — raw files are never modified.
2. **Durable contract** — the approved `MappingSpec` is the source of truth.
3. **Lineage by design** — every output column traces back to source, evidence,
   rules, artifacts, runs, and validations.
4. **LLM-assisted, human-gated** — the LLM proposes; humans approve.

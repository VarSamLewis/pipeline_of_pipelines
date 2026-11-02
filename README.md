# Pipeline of Pipelines

Auditable data-transformation backend. Clients upload heterogeneous raw files
(spreadsheets, PDFs, emails, text documents). The platform stores them
immutably, extracts evidence, builds versioned human-approved mapping
specifications, generates transformation artifacts, runs a Polars-based target
pipeline, and records full column-level lineage.

## Tech stack

- Python 3.13+
- FastAPI + Pydantic v2 + SQLModel
- PostgreSQL 16+ with `pgvector`
- Local filesystem object store first (MinIO/S3 later)
- Polars for target transformations
- OpenAI embeddings + OpenAI-compatible chat LLM for mapping proposals

## Project layout

```
backend/src/
  app.py          FastAPI routers
  models.py       Pydantic + SQLModel schemas
  file_ops.py     Object store abstraction and file utilities
  parser.py       Parsers and profilers for heterogeneous files
  db_ops.py       Database CRUD, evidence, lineage queries
  mapping.py      LLM-assisted mapping proposal
  codegen.py      Artifact and output-folder generation
  pipeline.py     Single-file Polars transformation runtime

tests/            pytest API, website, and CLI integration scenarios
examples/         Example client fixtures
```

## Installation

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency management.
From the project root:

```bash
uv pip install -e ".[dev]"
```

This installs the runtime dependencies plus `pytest`, `ruff`, and `mypy`.

### Nix development environment

The Nix flake supplies the development toolchain; Docker Compose continues to
run PostgreSQL with pgvector. Install Nix with flakes enabled, then run:

```bash
nix develop
cp .env.example .env
uv sync --frozen --extra dev
nix run .#db-up -- up -d postgres
nix run .#serve
```

The flake requires access to a running Docker daemon. On Linux, ensure Docker
is running and that your user can access it (for example, through the `docker`
group). Common helpers are:

```bash
nix run .#db-up -- up -d postgres
nix run .#db-down -- down
nix run .#test
nix run .#check
```

## Database

Start PostgreSQL + pgvector with Docker Compose:

```bash
docker compose up -d postgres
```

The default connection URL is:

```
postgresql+psycopg://postgres:postgres@localhost:5432/pipeline
```

## Running the server

Organizations add their own OpenAI-compatible API key through Settings before
uploading files or proposing mappings.

```bash
python -m uvicorn app:app --app-dir backend/src --reload
```

The API is then available at `http://127.0.0.1:8000`. Visit
`/docs` for the interactive OpenAPI UI.

## Typical workflow

1. **Create a client**

   ```bash
   curl -X POST http://127.0.0.1:8000/clients \
     -H "Content-Type: application/json" \
     -d '{"name": "Acme Corp", "code": "acme"}'
   ```

2. **Upload a target schema**

   ```bash
   curl -X POST "http://127.0.0.1:8000/clients/acme/target-schema" \
     -F "schema_file=@examples/complex_client/target_schema.json"
   ```

3. **Ingest a folder of raw files**

   ```bash
   # Local development only: server-path ingestion is disabled when deployed.
   curl -X POST "http://127.0.0.1:8000/clients/acme/ingest-folder" \
     -F "folder_path=examples/complex_client"
   ```

4. **Create a mapping spec, propose mappings, and approve**

   ```bash
   curl -X POST "http://127.0.0.1:8000/clients/acme/mapping-specs" \
     -H "Content-Type: application/json" \
     -d '{"source_raw_file_ids": ["<ids>"], "target_schema": {...}}'

   curl -X POST "http://127.0.0.1:8000/mapping-specs/<spec-id>/propose"

   curl -X POST "http://127.0.0.1:8000/mapping-specs/<spec-id>/approve" \
     -H "Content-Type: application/json" \
     -d '{"reviewer": "data-guardian"}'
   ```

5. **Generate the deliverable folder**

   ```bash
   curl -X POST "http://127.0.0.1:8000/mapping-specs/<spec-id>/output-folder"
   ```

   This produces exactly three files:

   - `pipeline.py` — standalone Polars transformation script
   - `mapping.json` — human- and machine-readable mapping spec
   - `results.csv` — transformed output

   Retrieve them at:

   ```bash
   curl http://127.0.0.1:8000/output-folders/<spec-id>/pipeline.py
   curl http://127.0.0.1:8000/output-folders/<spec-id>/mapping.json
    curl http://127.0.0.1:8000/output-folders/<spec-id>/results.csv
    ```

## CLI (development)

Use `pop` locally against the development API. `pop init` defaults to localhost;
pass the deployed service origin when using Cloud Run. Sign in through Clerk,
then use an existing client UUID:

```bash
uv run pop init --api-url https://<dev-service-url>
uv run pop login
uv run pop whoami
uv run pop upload --client <client-uuid> ./incoming/data.csv --manifest upload.json
uv run pop propose --client <client-uuid> --schema ./target_schema.json \
  --manifest upload.json --output mapping.json
# Review/edit mapping.json before proceeding.
uv run pop push mapping.json --dry-run
uv run pop push mapping.json
uv run pop run mapping.json --reviewer you@example.com --output-dir deliverables
```

To produce separate mappings and outputs from the **same uploaded files**, pass
two target schema files. Each is a separate review and approval gate:

```bash
uv run pop propose --client <client-uuid> --manifest upload.json \
  --schema ./customers_schema.json --schema ./orders_schema.json \
  --output-dir mappings
uv run pop push mappings/customers_schema-1-mapping.json
uv run pop push mappings/orders_schema-2-mapping.json
uv run pop run mappings/customers_schema-1-mapping.json \
  --reviewer you@example.com --output-dir output/customers
uv run pop run mappings/orders_schema-2-mapping.json \
  --reviewer you@example.com --output-dir output/orders
```

Every upload receives a timestamp and its own storage key, including repeat
uploads of the same filename. The `_timestamp` suffix is applied only to the
object name in storage; the original filename is retained for display.
If a schema has multiple target tables, its deliverables include one CSV per
table (`customers.csv`, `orders.csv`) instead of a single `results.csv`.
The website Upload form also accepts multiple schema JSON files, and the API
accepts `target_schemas: [{...}, {...}]` at
`POST /clients/{client_id}/mapping-specs`, returning `mapping_specs` with one
draft per schema. Existing `target_schema` requests still return a single draft.

`propose` calls the configured organization's LLM provider and can incur API
charges. `run` explicitly approves the mapping and executes it on the server.
Both upload manifests and proposed mappings refuse to overwrite existing local
files; use fresh paths for a new run. If a proposal fails after creating its
draft spec, retry with the same arguments plus `--spec <id>` from the error
message. See [the CLI design](docs/cli.md) for planned extensions.

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

Run the integration suite with a single command (requires Docker):

```bash
bash scripts/test-integration.sh
```

The script locates the repository regardless of the calling directory, builds
the CI image if needed, starts PostgreSQL, creates `pipeline_test` if needed,
and runs pytest in the CI image. GitHub CI and deployment workflows run this
same script.
The integration suite fails rather than silently skipping when PostgreSQL is
unavailable. Tests exercise the API, website, and CLI against that database;
only outbound LLM calls are stubbed. They do not run a browser or hit a deployed
service.

Run the complete local quality suite:

```bash
uv run ruff check backend/src tests scripts
uv run mypy backend/src
uv run pytest
git diff --check
```

Build the production container locally:

```bash
docker build --tag pipeline-of-pipelines:test .
```

Run Terraform validation after installing Terraform:

```bash
terraform -chdir=infra init -backend=false
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
```

See [`docs/production_runbook.md`](docs/production_runbook.md) for the GCP
smoke-test and deployment checklist.

## Configuration

Override the database URL with the `DATABASE_URL` environment variable:

```bash
DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname" \
  python -m uvicorn app:app --app-dir backend/src
```

There is no heuristic fallback: both ingestion (embeddings) and `/propose`
require an organization API key.

## Design principles

1. **Immutability** — raw files are never modified.
2. **Durable contract** — the approved `MappingSpec` is the source of truth.
3. **Lineage by design** — every output column traces back to source, evidence,
   rules, artifacts, runs, and validations.
4. **LLM-assisted, human-gated** — the LLM proposes; humans approve.

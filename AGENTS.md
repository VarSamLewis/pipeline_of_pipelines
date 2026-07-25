# Agent Notes — Pipeline of Pipelines

## Project overview

Backend for an auditable data-transformation platform. Clients upload
heterogeneous raw files (spreadsheets, PDFs, emails, text documents). The
platform stores them immutably, extracts evidence, builds versioned
human-approved mapping specifications, generates transformation artifacts, runs
a Polars-based target pipeline, and records full column-level lineage.

## Tech stack

- Python 3.13+
- FastAPI + Pydantic v2 + SQLModel
- PostgreSQL 16+ with `pgvector`
- Local filesystem object store first (MinIO/S3 later)
- Polars for target transformations
- dbt for optional SQL artifact generation
- OpenAI-compatible LLM for mapping proposals
- WorkOS AuthKit for authentication and role-based authorization

## Repository layout

```
backend/src/
  app.py          FastAPI routers
  auth_service.py WorkOS AuthKit authentication and role dependencies
  models.py       Pydantic + SQLModel schemas
  file_ops.py     Object store abstraction and file utilities
  parser.py       Parsers and profilers for heterogeneous files
  db_ops.py       Database CRUD, vector search, lineage queries
  mapping.py      LLM-assisted mapping proposal
  codegen.py      dbt/SQL artifact generation
  pipeline.py     Single-file Polars target transformation pipeline
  workflow.py     Simplified upload → review mapping → review results orchestration
  ui.py           HTMX UI for the three-step wizard
```

## Coding conventions

- Use `from __future__ import annotations` in every module.
- Type hints everywhere; prefer `uuid.UUID`, `dict[str, Any]`, `list[...]`.
- Keep functions pure where possible; I/O and side effects live in services.
- Docstrings follow Google style: one-line summary, Args, Returns.
- Raise `ValueError` for invalid inputs; let FastAPI handle HTTP exceptions.
- All database models inherit from `SQLModel` and live in `models.py`.
- Use `sa_column=Column(JSON)` for JSON fields in SQLModel.

## Dependencies

Dependencies are managed in `pyproject.toml` and installed with
[`uv`](https://docs.astral.sh/uv/):

```bash
uv pip install -e ".[dev]"
```

## Local development

Start the database with Docker Compose:

```bash
docker compose up -d postgres
```

Connection URL defaults to:

```
postgresql+psycopg://postgres:postgres@localhost:5432/pipeline
```

Set an OpenAI API key before running the server or tests:

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

For WorkOS authentication in production, configure:

```bash
export WORKOS_CLIENT_ID="client_..."
export WORKOS_API_KEY="sk_test_..."
export WORKOS_REDIRECT_URI="http://localhost:8000/auth/callback"
export SESSION_SECRET_KEY="$(python -c "import secrets; print(secrets.token_urlsafe(32))")"
```

Local scripts and tests bypass WorkOS when `AUTH_BYPASS_LOCAL=true`.
Roles are stored in WorkOS user metadata under the key `role` and synced on login.

## Testing

- Unit tests do not require a database.
- Integration tests require the Postgres container and an OpenAI key.
- Run tests with `pytest`.
- Integration tests are skipped automatically when Postgres is unreachable.

## Design principles

1. Immutability: raw files are never modified.
2. Durable contract: the approved `MappingSpec` is the source of truth.
3. Lineage by design: every output column traces back to source, evidence,
   rules, artifacts, runs, and validations.
4. LLM-assisted, human-gated: the LLM proposes; humans approve.

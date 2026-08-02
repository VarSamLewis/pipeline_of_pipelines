# Opportunities for improvement

Known issues, technical debt, and enhancement ideas, roughly ordered by
impact. Each item notes where the code lives and what a fix would involve.

## Known bugs / correctness

### 1. `ProposedMapping` never carries `confidence` (failing test)

`tests/test_mapping.py::test_parse_llm_mapping_response_builds_proposed_mappings`
fails with `AttributeError: 'ProposedMapping' object has no attribute
'confidence'`. The LLM payloads (and the test fixtures in `conftest.py`) include
a `confidence` score, but `parse_llm_mapping_response`
(`backend/src/mapping.py:561`) never reads it and `ProposedMapping`
(`backend/src/models.py:216`) has no field for it.

- Add `confidence: float | None` to `ProposedMapping`, parse it in
  `parse_llm_mapping_response`, and persist it on `MappingColumn`.
- Surface it in the mapping review UI so reviewers can triage low-confidence
  mappings.

### 2. `ValidationResult.passed == False` query relies on a comment-suppressed lint

`workflow.py:635` uses `ValidationResult.passed == False  # noqa: E712`.
This is intentional (SQLAlchemy boolean column vs Python `False`) and was
recently fixed from a latent bug, but it is fragile and easy to regress.
Consider a helper like `validation_failures(session, run_id)` in a repository
module to keep the SQLAlchemy comparison out of the workflow layer.

### 3. Hard-coded OpenAI model in the codegen retry path

`workflow.py:463` calls `call_codegen_llm(..., model="gpt-4o-mini", ...)`
instead of `settings.mapping_model`. The retry path can silently drift from the
configured model used everywhere else.

### 4. `db_ops` docstring says "SQLite for now", code targets Postgres

`db_ops.py:4` claims the initial implementation uses SQLite, but the code,
models (pgvector `Vector(1536)`), and docker-compose all require PostgreSQL.
Update the docstring and README wording. Postgres is already mandatory.

## Lint / type-check debt (pre-existing)

### 5. Unused variable `table_index_text`

`mapping.py:414` (`F841`) assigns `table_index_text` but never uses it in
`build_codegen_retry_prompt` (it is used in the mapping prompt). Either include
the table index in the retry prompt or drop the variable.

### 6. mypy strict errors in `workflow.py:retry_pipeline_and_execute`

Around lines 411-420: `RawFile` is treated as non-optional after the filter
(`session.get` returns `RawFile | None`) and around lines 446-453 a
`Sequence[BusinessRule]` is passed where `list[BusinessRule]` is expected.
Fix with `cast`/`assert` and an explicit `list(...)`.

### 7. Other `Any`-heavy modules

`routers/api.py`, `db_ops.py`, and `auth_service.py` lean on `Any` (and
`__import__` / local `from` imports) for import-cycle avoidance. This bypasses
type checking in the most data-critical paths. Options:

- Move shared session access into a dedicated module so the lazy imports go away.
- Use `TYPE_CHECKING` imports plus string annotations instead of runtime
  `__import__`.

## Concurrency / transactions

### 8. `get_engine()` returns a single shared engine

`db_ops.get_engine` caches one engine globally (intended), but `get_session()`
hands out raw sessions with no explicit context management. Callers frequently
interleave `session.add` / `session.commit` / `session.refresh` in workflow
loops (e.g. `record_validation_results`, `record_staging_metadata` commit per
row). A single commit per run plus bulk inserts would be far faster.

### 9. Evidence parsing embeds per-chunk in a loop

`_parse_raw_file` (`db_ops.py:313-326`) calls `get_embedding` (a network call)
per chunk and commits per chunk. For large files this is slow and chatty.
Batch embeddings (OpenAI supports arrays), and batch inserts.

## Reliability / observability

### 10. No migrations

`app.py` lifespan runs `create_tables()` (create-if-missing). `alembic` is a
dependency but unused. Schema drift across deploys will be painful. Add an
Alembic setup (or at least a documented migration strategy) before real data.

### 11. No background execution

Pipeline execution (`codegen.execute_generated_pipeline`) runs synchronously in
the request via `subprocess.run`. Long pipelines block the web worker and can
hit proxy/HTTP timeouts. Consider a worker/queue (e.g. background tasks,
Celery/RQ, or a durable job table) with async status polling.

### 12. `eval()` on generated validation expressions

`pipeline.run_validation_tests` uses `eval` on expressions built from target
schema + mapping columns. The generated pipeline.py avoids eval for
transformations (good); validation could use a similarly generated script or
Polars lazy expressions instead.

### 13. Silent `except Exception` in mapping review

`ui.py:146` swallows target-schema parse failures (`except Exception: pass`),
hiding broken specs. Log the error or render a warning.

### 14. Errors surfaced as HTTP 500 with raw LLM/runtime text

Several endpoints raise `HTTPException(500, detail=str(exc))` with raw error
strings. Sanitize, log server-side, and return stable error codes.

## Product / LLM quality

### 15. Confidence scores are requested but unused (see #1)

The mapping prompt does not even ask for confidence; the test fixtures include
it. Add it to the prompt + model + UI for reviewer triage.

### 16. Targeted evidence queries contain hard-coded cross-reference phrases

`mapping.py:268-274` hard-codes queries like `"region code mapping"`,
`"revenue calculation"`, `"test orders exclude"`. These are example-specific
and will not generalize. Derive cross-reference queries from the target schema
(enumerations, descriptions) instead.

### 17. Retry only tries once

`retry_pipeline_and_execute` regenerates the pipeline with the error and
re-executes a single time; if it fails again the user must retry manually.
Consider a bounded retry loop (max N) with escalation to human review.

### 18. Expression normalization is regex-based

`_normalize_polars_expression` (`mapping.py:504`) patches common LLM mistakes
with string/regex replacements. It is a pragmatic stopgap but brittle; a
semantic lint/validate of generated expressions (e.g. compile with Polars
against sample data) would be more robust.

## Architecture / DX

### 19. `ui.py` and `routers/api.py` drift

Two routers expose overlapping concepts (workflow steps) with different
conventions. The API layer is the "source of truth" documented flow; consider
having `ui.py` call the same service functions (it already does via
`workflow.py`) and trimming duplicated logic (e.g. storage-key/mime handling in
`api.upload_raw_file` vs `workflow._store_raw_file`).

### 20. README/module-list drift

The README's "Project layout" omits newer modules (`workflow.py`, `feedback.py`,
`mapping_specs.py`, `auth_service.py`, `ui.py`, `routers/`, `repositories/`,
`templates/`). Keep the README in sync (see the refreshed README) and ideally
point the CI lint at docs too.

### 21. Test coverage for the UI and chat flows is thin

`tests/` cover API, mapping, parser, codegen, auth, config, boundaries, and the
new chat-apply regression. There is no coverage for `mapping.html` rendering,
`chat_sidebar.html`, `results.html`/CSV pagination, `feedback.propose_refinements`
against a mocked LLM, or `retry_pipeline_and_execute`. Add a `mock_openai`
variant that returns refinements.

### 22. No end-to-end test of the generated pipeline

`test_codegen_catalog.py` covers source loading; there is no test that runs
`generate_output_folder` end-to-end against `examples/complex_client` and
asserts the CSV shape. That would catch codegen/dtype regressions cheaply.

## Nits

- `models.py` uses deprecated `datetime.utcnow` as the SQLModel default across
  many tables while new code uses `datetime.now(UTC)`; standardize on UTC.
- `_user_context` in `ui.py` returns a fixed dict; minor, but could accept
  extra context to avoid repetition.
- `routers/api.py` uses `__import__("sqlmodel").select(...)` and
  `__import__("auth_service").authenticate_with_workos(...)` — replace with
  proper imports now that import cycles are understood.
- `config.Settings` mixes infra config and local path derivation; tests build
  `Settings.from_env` with `project_root` overrides — fine, but consider
  splitting "paths" from "runtime config" if Azure adapters land.
- `data/` git-ignored contents (object-store, output-folders) are written
  under `data/`; ensure `.env` and secrets stay out of version control.

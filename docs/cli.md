# CLI design

The `pop` command-line interface provides the same data-transformation workflow
as the web application for users who need local files, repeatable commands, or
automation. It is intentionally split into explicit stages so that mappings can
be reviewed before code generation and execution.

## Workflow

```text
local source files
  -> pop upload
  -> pop propose
  -> edit mapping.json
  -> pop push
  -> pop run
  -> pipeline.py, mapping.json, results.csv
```

`pop upload` only stores files. It does not parse, propose a mapping, generate
code, or execute a pipeline. Mapping proposal, mapping edits, and execution are
separate, deliberate actions.

## Commands

### Upload source files

`upload` accepts one or more local files as positional arguments. Shell glob
expansion is supported by the user's shell.

```bash
pop upload --client <client-id> ./incoming/orders.xlsx ./incoming/notes.pdf
pop upload --client <client-id> ./incoming/* --manifest ./upload.json
```

The command creates one ingestion batch, uploads every supplied file, and
writes a manifest. The default manifest location is printed at the end of the
command; `--manifest` selects an explicit path.

Before uploading, the CLI checks that paths are regular files. The server
enforces file-count and size limits. The manifest is updated after each
successful upload, preserving the file IDs if a later upload fails. Use a new
manifest path for each batch.

Example upload manifest:

```json
{
  "version": 1,
  "client_id": "c7223300-87b3-4d0a-94d1-c5a1d07b8e6c",
  "batch_id": "12b8c5eb-5b9e-48e2-a0ac-553fe10b95d",
  "files": [
    {
      "id": "31a6ffb8-a24b-4bfb-8eaa-ee2f84c79af2",
      "path": "./incoming/orders.xlsx",
      "sha256": "..."
    }
  ]
}
```

### Propose a mapping

`propose` parses uploaded files, stores the supplied target schema, creates
a mapping specification, and requests a proposed mapping. It writes the
proposal to a local JSON file for review and editing.

```bash
pop propose \
  --client <client-id> \
  --schema ./target_schema.json \
  --manifest ./upload.json \
  --output ./mapping.json
```

Run the same command again to refresh a proposal. If `mapping.json` already
exists, the CLI checks that its server-side spec belongs to the supplied client,
schema, and upload manifest. Draft/proposed specs are retried; finalized specs
get a new, independent review. The local file is replaced only after success.
Use `--spec <id>` to resume a draft when a previous attempt failed before it
could write `mapping.json`.

The output document includes the server-side mapping specification ID and
mapping-column IDs. Those identifiers let the CLI apply local edits to the
correct immutable source files and mapping records.

Repeat `--schema` with `--output-dir` to create separate specs for multiple
target schemas from the same manifest; review, push, and run each mapping
independently. If one target schema has multiple tables, its output directory
contains a CSV per table instead of `results.csv`.

### Edit and push a mapping

Review and edit `mapping.json` in an editor or through a future guided CLI
editor. Then validate and apply the edits:

```bash
pop push ./mapping.json
```

`push` validates the document, compares it with the stored mapping, and
updates only changed mapping columns. It does not approve the mapping or run
the generated pipeline.

Use `--dry-run` to show planned changes without writing them:

```bash
pop push ./mapping.json --dry-run
```

### Generate and run

Once the mapping is ready, `run` approves it, generates the pipeline, runs
the transformation, and downloads the client deliverables.

```bash
pop run ./mapping.json \
  --reviewer "sam@example.com" \
  --output-dir ./deliverables
```

The output directory contains exactly these deliverables:

```text
deliverables/
  pipeline.py
  mapping.json
  results.csv
```

The command prints the execution-run ID and local output directory. It does not
concatenate artifacts on standard output. The `show`, `code`, and `results`
convenience commands are not implemented yet.

## Command principles

- **Explicit stages:** upload, proposal, edits, and execution are separate so
  human review remains a gate.
- **Local files in; local deliverables out:** positional upload paths make
  normal shell use simple, while `--output-dir` provides unambiguous multi-file
  results.
- **Machine-readable review:** proposal and upload manifests are JSON files;
  a `--output json` option for commands remains future work.
- **Retry awareness:** manifests record successful file IDs, but failed
  proposals and executions are not automatically resumed yet.
- **No secret persistence by default:** provider credentials are never written
  into manifests or mapping files.

## Authentication and current limitation

The CLI uses Clerk device authorization and sends the resulting OAuth access
token to the API. Browser sessions continue to use signed HTTP-only cookies.
Tokens are stored in the operating-system keychain, not upload manifests.

# CLI design

The `pop` command-line interface provides the same data-transformation workflow
as the web application for users who need local files, repeatable commands, or
automation. It is intentionally split into explicit stages so that mappings can
be reviewed before code generation and execution.

## Workflow

```text
local source files
  -> pop upload
  -> pop map propose
  -> edit mapping.json
  -> pop map push
  -> pop map run
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

Before uploading, the CLI validates that every path is a readable regular file
and checks supported extensions, file-count limits, individual file-size
limits, and total batch-size limits. It reports upload progress and preserves
the successfully uploaded file IDs if a later file fails.

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
      "filename": "orders.xlsx",
      "sha256": "..."
    }
  ]
}
```

### Propose a mapping

`map propose` parses uploaded files, stores the supplied target schema, creates
a mapping specification, and requests a proposed mapping. It writes the
proposal to a local JSON file for review and editing.

```bash
pop map propose \
  --client <client-id> \
  --schema ./target_schema.json \
  --manifest ./upload.json \
  --output ./mapping.json
```

Files can be supplied directly when a manifest is not desired:

```bash
pop map propose \
  --client <client-id> \
  --schema ./target_schema.json \
  <raw-file-id> <raw-file-id> \
  --output ./mapping.json
```

The output document includes the server-side mapping specification ID and
mapping-column IDs. Those identifiers let the CLI apply local edits to the
correct immutable source files and mapping records.

### Edit and push a mapping

Review and edit `mapping.json` in an editor or through a future guided CLI
editor. Then validate and apply the edits:

```bash
pop map push ./mapping.json
```

`map push` validates the document, compares it with the stored mapping, and
updates only changed mapping columns. It does not approve the mapping or run
the generated pipeline.

Use `--dry-run` to show planned changes without writing them:

```bash
pop map push ./mapping.json --dry-run
```

### Generate and run

Once the mapping is ready, `map run` approves it, generates the pipeline, runs
the transformation, and downloads the client deliverables.

```bash
pop map run ./mapping.json \
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

The command prints the mapping ID, execution-run ID, validation status, and
paths to the downloaded files. It must not concatenate code and CSV on standard
output. Consumers that need one artifact on standard output should use focused
artifact commands:

```bash
pop map show ./mapping.json --output json
pop map code ./mapping.json > pipeline.py
pop map results ./mapping.json > results.csv
```

## Command principles

- **Explicit stages:** upload, proposal, edits, and execution are separate so
  human review remains a gate.
- **Local files in; local deliverables out:** positional upload paths make
  normal shell use simple, while `--output-dir` provides unambiguous multi-file
  results.
- **Machine-readable automation:** every command supports `--output json`;
  human-oriented output is the default.
- **Safe retries:** manifests, mapping IDs, checksums, and idempotency-aware
  operations make it possible to resume or repeat steps without guessing.
- **No secret persistence by default:** provider credentials are never written
  into manifests or mapping files.

## Authentication and current limitation

The deployed application currently authenticates API requests with a signed,
HTTP-only browser session cookie. A CLI cannot safely acquire or store that
cookie after a browser login. Therefore, the authenticated production CLI
commands require a future browser-login/device-authorization flow and bearer
token support in the backend.

The CLI command structure, local validation, manifest handling, and local
development use can be implemented now. Until API token support exists,
production users should complete the authenticated workflow in the web
application rather than copying browser cookies into CLI configuration.

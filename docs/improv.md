UI

- Done - Remove Reviewers from settings page
- Done - Client context on homepage - should just be client
- Done - Add panel to see client files
- Done - Make runs a seperate page and show all prev runs within an org
- Done - Wrap JSON/code/csv
- Improve error messages for end users
- Change timestamps to 0 dp
- Revamp entire to panel format UI

Applications

- Done - Seperate roles - platform admin (just me), users (can see all clients), org admins (can edit api keys)
- - Clean code base
- Done - Enforce organisation membership on UI routes, so invited users only access their organisation’s data.
- Done - Provide reliable access revocation, including existing sessions.
- Done - Prevent arbitary code execution on cloud run
- Add progressive retry for Google Drive and Sheets read imports: bounded exponential backoff with jitter for transient network failures, 429s (honour Retry-After), and 5xx responses; keep OAuth code exchange non-retriable and use import audit/idempotency records to prevent duplicate snapshots.

Data Storage

- Done - In cloud storage bucket create a clients/{client_name}/{file_name}\_timestamp.extension
- Validate that the seperation in the DB actually works
- Version client context by immutable chunks: parse each upload, compare new chunks only with that client's active chunks, and atomically supersede confident matches; keep unmatched old chunks active and files as provenance.
- Retrieve only active chunks for new mappings, pin the exact chunk IDs used for audit, and integration-test partial updates, ambiguous matches, tenant isolation, and older mapping reproducibility.

Infra

- Improve docker build step in CICD
- Seperate cloud run instance
- Remove seperate envs
- Cost optimisation
- Scale up DB server???

"""Credential-free execution service for unreviewed Polars candidates.

Deploy this app separately from the main API, with a service account that has no
project permissions and with all outbound network traffic blocked. Never mount
application secrets or the production database into this service.
"""

from __future__ import annotations

import base64
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_SCRIPT_BYTES = 100 * 1024
TIMEOUT_SECONDS = 35
_SAFE_NAME = re.compile(r"^[0-9a-fA-F-]{36}_[^/\\\x00]{1,180}$")


class CandidateRequest(BaseModel):
    """The only data the unprivileged worker receives from the application."""

    script: str = Field(max_length=MAX_SCRIPT_BYTES)
    mapping: dict[str, Any]
    sources: dict[str, str]  # original snapshot names -> base64 encoded bytes


class CandidateResult(BaseModel):
    passed: bool
    error: str | None = None
    failure_type: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _limit_process() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _exit_worker() -> None:
    """Never reuse a container after it has executed model-authored code."""
    # Allow the HTTP proxy to flush the completed response. Concurrency=1 and
    # the still-running background task keep this instance unavailable to peers.
    time.sleep(0.25)
    os._exit(0)


@app.post("/execute")
def execute_candidate(request: CandidateRequest) -> JSONResponse:
    try:
        result = _execute(request)
    except HTTPException:
        raise  # Rejected before any candidate code ran.
    except Exception:
        result = CandidateResult(
            passed=False,
            error="Worker could not finish preview",
            failure_type="WorkerError",
        )
    return JSONResponse(result.model_dump(), background=BackgroundTask(_exit_worker))


def _execute(request: CandidateRequest) -> CandidateResult:
    """Execute one candidate against bounded inputs in a disposable directory."""
    if os.environ.get("SANDBOX_WORKER") != "true":
        raise HTTPException(503, "Isolated worker is not configured")
    if len(request.script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise HTTPException(413, "Candidate script exceeds the limit")
    if not request.sources:
        raise HTTPException(422, "Candidate requires source snapshots")
    sources: dict[str, bytes] = {}
    total = 0
    for name, encoded in request.sources.items():
        if not _SAFE_NAME.fullmatch(name):
            raise HTTPException(422, "Invalid source snapshot name")
        if len(encoded) > (MAX_SOURCE_BYTES * 4 // 3 + 8):
            raise HTTPException(413, "Source snapshot exceeds the limit")
        try:
            source = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "Invalid source snapshot") from exc
        total += len(source)
        if len(source) > MAX_SOURCE_BYTES or total > MAX_TOTAL_SOURCE_BYTES:
            raise HTTPException(413, "Source snapshots exceed the limit")
        sources[name] = source

    with tempfile.TemporaryDirectory(prefix="candidate-") as temporary:
        root = Path(temporary)
        source_folder = root / "sources"
        output_folder = root / "outputs"
        source_folder.mkdir()
        output_folder.mkdir()
        for name, data in sources.items():
            (source_folder / name).write_bytes(data)
        script = root / "pipeline.py"
        script.write_text(request.script, encoding="utf-8")
        (root / "mapping.json").write_text(
            json.dumps(request.mapping), encoding="utf-8"
        )
        # No inherited credentials or provider configuration reach model code.
        environment = {
            "PATH": os.path.dirname(sys.executable),
            "HOME": temporary,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with (root / "stderr").open("wb") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--source-folder",
                    str(source_folder),
                    "--output-folder",
                    str(output_folder),
                ],
                cwd=root,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=True,
                preexec_fn=_limit_process,
            )
            try:
                return_code = process.wait(timeout=TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return CandidateResult(
                    passed=False, error="Candidate exceeded time limit"
                )
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if return_code:
            # The caller never forwards stderr or source rows to the LLM.
            error = (root / "stderr").read_text(errors="replace")[-4000:]
            types = re.findall(
                r"(?m)^([\w.]+(?:Error|Exception))(?=:)", error
            )
            failure_type = types[-1].rsplit(".", 1)[-1] if types else "ProcessError"
            return CandidateResult(
                passed=False,
                error=f"Exit {return_code}: {error}",
                failure_type=failure_type,
            )
        outputs: dict[str, str] = {}
        output_bytes = 0
        for path in sorted(output_folder.glob("*.csv")):
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            output_bytes += size
            if output_bytes > MAX_OUTPUT_BYTES:
                return CandidateResult(
                    passed=False, error="Candidate output exceeds limit"
                )
            outputs[path.name] = base64.b64encode(path.read_bytes()).decode("ascii")
        if not outputs:
            return CandidateResult(
                passed=False, error="Candidate produced no CSV output"
            )
        return CandidateResult(passed=True, outputs=outputs)

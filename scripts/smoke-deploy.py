"""Verify a deployed service responds after infrastructure rollout."""

from __future__ import annotations

import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main(url: str, worker_url: str | None = None) -> None:
    health_url = f"{url.rstrip('/')}/health"
    for attempt in range(12):
        try:
            with urlopen(health_url, timeout=10) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    print(f"Deployment healthy: {health_url}")
                    break
                print(f"Unexpected health response: HTTP {response.status}")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            print(f"Health check {attempt + 1}/12 failed: {exc}")
        if attempt < 11:
            time.sleep(10)
    else:
        raise SystemExit(f"Deployment failed smoke check: {health_url}")

    with urlopen(f"{url.rstrip('/')}/capabilities", timeout=10) as response:
        capabilities = json.load(response)
    print(f"Capabilities: {capabilities}")

    if worker_url:
        request = Request(
            f"{worker_url.rstrip('/')}/execute",
            data=b'{"script":"pass","mapping":{},"sources":{}}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                raise SystemExit(
                    f"Candidate worker accepted anonymous execution: "
                    f"HTTP {response.status}"
                )
        except HTTPError as exc:
            if exc.code != 403:
                raise SystemExit(
                    f"Candidate worker smoke check failed: HTTP {exc.code}"
                ) from exc
            print(f"Candidate worker rejects anonymous execution: {worker_url}")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or not sys.argv[1].startswith("https://"):
        raise SystemExit(
            "Usage: smoke-deploy.py https://deployed-service-url [worker-url]"
        )
    main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)

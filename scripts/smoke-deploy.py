"""Verify a deployed service responds after infrastructure rollout."""

from __future__ import annotations

import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def main(url: str) -> None:
    health_url = f"{url.rstrip('/')}/health"
    for attempt in range(12):
        try:
            with urlopen(health_url, timeout=10) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    print(f"Deployment healthy: {health_url}")
                    return
                print(f"Unexpected health response: HTTP {response.status}")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            print(f"Health check {attempt + 1}/12 failed: {exc}")
        if attempt < 11:
            time.sleep(10)
    raise SystemExit(f"Deployment failed smoke check: {health_url}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        raise SystemExit("Usage: smoke-deploy.py https://deployed-service-url")
    main(sys.argv[1])

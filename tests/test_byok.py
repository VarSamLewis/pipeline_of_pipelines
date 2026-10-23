"""Tests for organization-owned provider credential validation."""

from __future__ import annotations

import pytest
from byok import validate_provider_base_url


def test_provider_base_url_accepts_public_https_endpoint() -> None:
    """A public HTTPS OpenAI-compatible endpoint is stored without a slash."""
    assert (
        validate_provider_base_url("https://api.example.com/v1/")
        == "https://api.example.com/v1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.1/v1",
    ],
)
def test_provider_base_url_rejects_non_public_endpoints(url: str) -> None:
    """Provider URLs cannot target insecure or local infrastructure."""
    with pytest.raises(ValueError):
        validate_provider_base_url(url)

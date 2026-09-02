"""OpenAI / OpenAI-compatible LLM client factory.

Returns an :class:`openai.AzureOpenAI` client when Azure OpenAI is configured,
so that model names resolve directly to Azure deployment names. Otherwise it
returns the plain :class:`openai.OpenAI` client against the configured base
URL (or an OpenAI-compatible provider supplied via ``base_url``).
"""

from __future__ import annotations

from typing import Any

from config import Settings, get_settings


def build_client(
    api_key: str | None = None,
    base_url: str | None = None,
    settings: Settings | None = None,
) -> Any:
    """Build an OpenAI-compatible client.

    Args:
        api_key: Override for ``OPENAI_API_KEY`` (ignored in Azure mode).
        base_url: Override for ``OPENAI_BASE_URL`` (ignored in Azure mode).
        settings: Settings override for tests.

    Returns:
        A configured ``OpenAI``/``AzureOpenAI`` client instance.
    """
    settings = settings or get_settings()

    if settings.azure_openai_endpoint and settings.azure_openai_api_key:
        from openai import AzureOpenAI

        return AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )

    resolved_key = api_key or settings.openai_api_key
    if not resolved_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    from openai import OpenAI

    return OpenAI(
        api_key=resolved_key,
        base_url=base_url or settings.openai_base_url,
    )

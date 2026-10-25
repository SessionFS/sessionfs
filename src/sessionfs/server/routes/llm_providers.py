"""First-class LLM provider registry routes.

Exposes the provider table from ``judge/provider_registry.py`` to the
dashboard and shares the one place provider ids are validated against. A
provider id that is not in the registry is a client error (422) unless the
request also carries a custom ``base_url`` — a caller-supplied endpoint keeps
working for any provider id because the endpoint, not the registry, is what
routes the request.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from sessionfs.judge.provider_registry import get_provider, provider_specs
from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.models import User

router = APIRouter(prefix="/api/v1/llm", tags=["llm-providers"])


def validate_provider_or_422(provider: str, base_url: str | None = None) -> None:
    """422 ``unknown_provider`` for a provider id absent from the registry.

    A custom ``base_url`` opts out of registry validation: the request is
    routed by the caller-supplied endpoint, so the provider id is only a
    label there (matches the historical custom-endpoint behaviour).
    """
    if base_url:
        return
    if get_provider(provider) is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_provider",
                "message": f"Unknown LLM provider: {provider}",
            },
        )


@router.get("/providers")
async def list_llm_providers(
    user: User = Depends(get_current_user),
) -> dict:
    """List the first-class LLM providers available to BYOK settings.

    Any signed-in user may read this: it exposes no secrets, only the
    provider table the judge/helper pickers render.
    """
    return {"providers": provider_specs()}

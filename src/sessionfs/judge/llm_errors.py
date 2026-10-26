"""Readable classification of LLM provider failures.

One place turns an upstream HTTP failure (status, body, content type) into a
stable ``code`` plus a plain-English message. The connection test
(``judge/connection_test.py``), the streaming layer (``agent_stream``) and
the single-shot calls (``providers``) all use it, so a misconfigured Base URL
reads the same everywhere: a console address such as
``https://platform.deepseek.com`` answers ``405 Method Not Allowed``, and the
user sees "use https://api.deepseek.com" instead of the raw upstream body.

Messages never contain the API key or a raw upstream body; only
``provider_error`` carries a short, sanitised snippet of the provider's own
error message.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

from sessionfs.judge.provider_registry import PROVIDERS, Provider, get_provider

# Stable failure codes (also the dashboard's switch keys).
ENDPOINT_NOT_API = "endpoint_not_api"
INVALID_KEY = "invalid_key"
ACCESS_DENIED = "access_denied"
MODEL_NOT_FOUND = "model_not_found"
RATE_LIMITED = "rate_limited"
INSUFFICIENT_BALANCE = "insufficient_balance"
ENDPOINT_BLOCKED = "endpoint_blocked"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
PROVIDER_ERROR = "provider_error"

# Failures that can mean the saved configuration never works. A save is
# refused for INVALID_KEY / ENDPOINT_BLOCKED, and for ENDPOINT_NOT_API only
# when the reply is definitive (see ``is_definitive_not_api``); everything
# else is treated as transient and saved with a warning.
BLOCKING_CODES = frozenset({ENDPOINT_NOT_API, INVALID_KEY, ENDPOINT_BLOCKED})

_MAX_SNIPPET = 200
_SCAN_CHARS = 4000

_BALANCE_TOKENS = (
    "insufficient balance",
    "insufficient_balance",
    "insufficient_quota",
    "insufficient credit",
    "insufficient funds",
    "out of credit",
    "no credit",
    "credit balance",
    "balance is too low",
    "exceeded your current quota",
    "payment required",
)
_MODEL_TOKENS = (
    "not found",
    "not_found",
    "does not exist",
    "not exist",
    "no such model",
    "unknown model",
    "invalid model",
    "model_not_found",
    "not available",
    "is not supported",
    "unsupported model",
    # DeepSeek: "The supported API model names are ..., but you passed x."
    "model names are",
    "but you passed",
)
# Wording that clearly says the key itself is bad (not a permission issue).
_KEY_TOKENS = (
    "api key not valid",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "api_key_invalid",
    "authentication fail",
    "invalid authentication",
    "invalid x-api-key",
)


class LlmEndpointError(httpx.HTTPStatusError):
    """An upstream failure with a readable ``code`` and ``friendly_message``.

    Subclasses ``HTTPStatusError`` so existing ``except httpx.HTTPStatusError``
    handlers keep catching it; handlers that know about it show
    ``friendly_message`` instead of the upstream body.
    """

    def __init__(
        self,
        code: str,
        friendly_message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        definitive: bool = True,
    ) -> None:
        super().__init__(friendly_message, request=request, response=response)
        self.code = code
        self.friendly_message = friendly_message
        self.definitive = definitive


def json_error_message(raw: str) -> str | None:
    """The provider's own message from a JSON error body, else ``None``.

    Only ``error.message`` / ``error`` (string) / ``message`` / ``detail``
    fields count; a non-JSON body (an HTML page, a proxy's plain text) is
    never echoed back to users.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg
    elif isinstance(err, str) and err.strip():
        return err
    for field in ("message", "detail"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def extract_error_message(raw: str) -> str:
    """The provider's message, falling back to the raw body (for matching
    only — use ``json_error_message`` for anything shown to users)."""
    return json_error_message(raw) or raw.strip() or "unknown error"


def sanitise_snippet(message: str, api_key: str = "", limit: int = _MAX_SNIPPET) -> str:
    """Strip the key and control characters, and cap the length."""
    if api_key:
        message = message.replace(api_key, "***")
    message = "".join(ch if ch.isprintable() else " " for ch in message).strip()
    if len(message) > limit:
        message = message[:limit] + "..."
    return message or "unknown error"


def looks_like_html(body: str, content_type: str = "") -> bool:
    if "text/html" in (content_type or "").lower():
        return True
    head = body.lstrip()[:20].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def _host_and_path(url: str | None) -> tuple[str, str]:
    if not url:
        return "", ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "", ""
    return (parsed.hostname or "").lower(), parsed.path or ""


def _api_url_hint(profile: Provider) -> str | None:
    """The Base URL to suggest for a provider, when one can be typed in.

    Only OpenAI-compatible providers: a Base URL is always called with the
    OpenAI chat-completions format, so Anthropic/Google are reached by
    leaving the field empty instead.
    """
    if profile.api_style == "openai":
        return profile.base_url
    return None


def _console_match(host: str) -> Provider | None:
    for profile in PROVIDERS:
        if host and host in profile.console_hosts:
            return profile
    return None


def _api_host_match(host: str) -> Provider | None:
    for profile in PROVIDERS:
        if host and host == _host_and_path(profile.base_url)[0]:
            return profile
    return None


def endpoint_not_api_message(
    base_url: str | None, provider: str | None = None, *, definitive: bool = True
) -> str:
    """The readable message for a Base URL that doesn't serve chat requests.

    ``definitive`` is False for a plain JSON 404 from an unknown host (it
    may be a gateway's route or a transient misconfiguration), which gets a
    softer lead.
    """
    host, _ = _host_and_path(base_url)
    lead = (
        "This address doesn't accept chat requests."
        if definitive
        else "This address answered 404 Not Found to a chat request; it may not be an API address."
    )
    console = _console_match(host)
    if console is not None:
        hint = _api_url_hint(console)
        if hint:
            return (
                f"{lead} {host} is {console.label}'s web console, not its API. "
                f"Use {hint}, or leave Base URL empty to use {console.label}'s API."
            )
        return (
            f"{lead} {host} is {console.label}'s web console, not its API. "
            f"Leave Base URL empty to use {console.label}'s API."
        )
    same_host = _api_host_match(host)
    if same_host is not None:
        hint = _api_url_hint(same_host)
        if hint:
            return (
                f"{lead} Use {same_host.label}'s API address ({hint}), or leave "
                f"Base URL empty to use {same_host.label}'s API."
            )
        return f"{lead} Leave Base URL empty to use {same_host.label}'s API."
    profile = get_provider(provider or "")
    if profile is not None:
        hint = _api_url_hint(profile)
        if hint:
            return (
                f"{lead} Use the provider's API address (for {profile.label}: {hint}), "
                "or leave Base URL empty."
            )
    return (
        f"{lead} Use the provider's API address (for DeepSeek: https://api.deepseek.com), "
        "or leave Base URL empty."
    )


def _not_api_status(status: int) -> bool:
    """Statuses where a web page means "this isn't an API" (not an outage)."""
    return 200 <= status < 300 or status in (404, 405)


def classify_http_failure(
    status: int,
    body: str,
    content_type: str = "",
    *,
    base_url: str | None = None,
) -> str:
    """Map an upstream HTTP failure to a failure code.

    ``base_url`` is the caller-supplied endpoint (``None`` for a registry
    provider): only a custom endpoint can be "not an API". An HTML page only
    means that on 2xx/404/405 — a proxy's 502/503 page or a 429 page is an
    outage or a rate limit and is classified as such.
    """
    custom = bool(base_url)
    text = body[:_SCAN_CHARS]
    low = text.lower()
    message_low = extract_error_message(text).lower()
    html = looks_like_html(text, content_type)
    if status == 401:
        return INVALID_KEY
    if status == 403:
        # A WAF / CDN block page is not a key rejection, and a JSON 403 is
        # usually a permission problem (region, model access, org policy)
        # unless it plainly says the key is invalid.
        if html:
            return PROVIDER_ERROR
        return INVALID_KEY if any(tok in low for tok in _KEY_TOKENS) else ACCESS_DENIED
    if status == 402 or any(tok in low for tok in _BALANCE_TOKENS):
        return INSUFFICIENT_BALANCE
    if status == 429:
        return RATE_LIMITED
    if custom and html and _not_api_status(status):
        return ENDPOINT_NOT_API
    if status == 405 and custom:
        return ENDPOINT_NOT_API
    model_ish = (
        "model" in message_low
        and "parameter" not in message_low
        and any(tok in low for tok in _MODEL_TOKENS)
    )
    if status == 404:
        if model_ish:
            return MODEL_NOT_FOUND
        return ENDPOINT_NOT_API if custom else PROVIDER_ERROR
    if status in (400, 422):
        if any(tok in low for tok in _KEY_TOKENS) and "model" not in message_low:
            return INVALID_KEY
        if model_ish:
            return MODEL_NOT_FOUND
    return PROVIDER_ERROR


def is_console_host(base_url: str | None) -> bool:
    host, _ = _host_and_path(base_url)
    return _console_match(host) is not None


def is_definitive_invalid_key(status: int, body: str) -> bool:
    """Whether an ``invalid_key`` reply is certain enough to refuse any save.

    A 401, or a 400 that plainly says the key is invalid (Google). A 403
    that says so only refuses a save that also changes the Base URL.
    """
    if status == 401:
        return True
    low = body[:_SCAN_CHARS].lower()
    return status == 400 and any(tok in low for tok in _KEY_TOKENS)


def is_definitive_not_api(
    status: int, body: str, content_type: str = "", *, base_url: str | None = None
) -> bool:
    """Whether an ``endpoint_not_api`` reply is certain enough to refuse a save.

    Certain: 405, a web page on 2xx/404/405, a redirect (requests never
    follow one), or any reply from a known console host. A bare JSON 404
    from an unknown host is not — it may be a gateway route that is
    briefly missing — so it is saved with a warning.
    """
    if not base_url:
        return False
    if is_console_host(base_url):
        return True
    if status == 405 or 300 <= status < 400:
        return True
    return looks_like_html(body[:_SCAN_CHARS], content_type) and _not_api_status(status)


def friendly_message(
    code: str,
    *,
    base_url: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    host: str | None = None,
    key_sent: bool = True,
    detail: str | None = None,
    timeout_seconds: float = 15,
    definitive: bool = True,
) -> str:
    """Plain-English message for a failure code.

    ``detail`` must already be a sanitised snippet from a JSON error field
    (``json_error_message``); raw bodies are never passed here.
    """
    if code == ENDPOINT_NOT_API:
        return endpoint_not_api_message(base_url, provider, definitive=definitive)
    if code == INVALID_KEY:
        if not key_sent:
            return "This endpoint needs an API key. Enter the key and try again."
        return (
            "The provider rejected the API key. Check that it is correct, still "
            "active, and belongs to this provider."
        )
    if code == ACCESS_DENIED:
        return (
            "The provider refused this request (access, region or model permissions). "
            "Check that this key may use this model from where SessionFS runs."
        )
    if code == MODEL_NOT_FOUND:
        name = f'"{model}"' if model else "This model"
        return (
            f"The model {name} isn't available with this key. Check the model "
            "name, or fetch the list of models."
        )
    if code == RATE_LIMITED:
        return "The provider is rate limiting this key. Wait a moment and try again."
    if code == INSUFFICIENT_BALANCE:
        return (
            "The provider says this account has no balance or credit left. Top it "
            "up, then try again."
        )
    if code == ENDPOINT_BLOCKED:
        return (
            "This address isn't allowed. It must use https and resolve to a public "
            "address (self-hosted servers can allow private hosts with "
            "SFS_LLM_ALLOW_PRIVATE_HOSTS)."
        )
    if code == TIMEOUT:
        return (
            f"The provider didn't answer within {int(timeout_seconds)} seconds. Try "
            "again; if it keeps happening, check the Base URL."
        )
    if code == UNREACHABLE:
        where = host or "the endpoint"
        return f"Couldn't connect to {where}. Check the address and that the server is online."
    if detail:
        return f"The provider returned an error: {detail}"
    return "The provider returned an error."


def endpoint_error_from_response(
    resp: httpx.Response,
    *,
    base_url: str | None,
    provider: str | None = None,
) -> LlmEndpointError | None:
    """An ``LlmEndpointError`` when a custom endpoint's reply isn't an API's.

    Used by the single-shot path (audit, narrative summaries) so a console
    URL surfaces the readable message, not ``405 {"detail": ...}``.
    """
    if not base_url:
        return None
    try:
        body = resp.content[:_SCAN_CHARS].decode("utf-8", errors="replace")
    except Exception:
        body = ""
    content_type = resp.headers.get("content-type", "")
    status = resp.status_code
    if status < 400:
        if not (looks_like_html(body, content_type) or 300 <= status < 400):
            return None
        code = ENDPOINT_NOT_API
    else:
        code = classify_http_failure(status, body, content_type, base_url=base_url)
    if code != ENDPOINT_NOT_API:
        return None
    definitive = is_definitive_not_api(status, body, content_type, base_url=base_url)
    return LlmEndpointError(
        code,
        endpoint_not_api_message(base_url, provider, definitive=definitive),
        request=resp.request,
        response=resp,
        definitive=definitive,
    )

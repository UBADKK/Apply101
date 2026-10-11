import os

from openai import OpenAI, OpenAIError, RateLimitError


OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# Generic, key-free error text shared by every analysis route that needs
# OpenAI. Never include the key or any raw exception text in a response.
ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL = "Analysis service is not configured."

# OpenAI 429 error codes meaning the project/organization spend or credit
# limit is exhausted -- not a transient per-minute rate limit.
PROVIDER_SPEND_LIMIT_ERROR_CODES = frozenset({
    "project_spend_limit_exceeded",
    "organization_spend_limit_exceeded",
    "credit_balance_exhausted",
    "organization_usage_limit_exceeded",
})


def create_openai_client_or_none() -> OpenAI | None:
    """Returns an OpenAI client, or None when analysis is not configured.

    Requires a non-blank OPENAI_API_KEY: the analysis seams call the
    Responses API, which authenticates only with that key. The SDK itself
    would still construct a client from OPENAI_ADMIN_KEY alone or from a
    whitespace-only key, and the request would then fail only after the
    analysis guard was acquired -- so those cases are treated as "not
    configured" here instead.

    With a key present this is exactly the previous `OpenAI()`
    construction, so analysis behavior is unchanged. Neither the key nor
    any SDK error text is logged or returned; construction performs no
    network call.
    """
    if not (os.environ.get(OPENAI_API_KEY_ENV) or "").strip():
        return None
    try:
        return OpenAI()
    except OpenAIError:
        return None


def provider_spend_limit_error_code(exc: BaseException) -> str | None:
    """Returns the matched spend-limit code when `exc` is an OpenAI SDK 429
    RateLimitError whose error code is in PROVIDER_SPEND_LIMIT_ERROR_CODES;
    otherwise None. Never raises: a missing, None or non-str code (the SDK
    does not guarantee a str) is simply not a match.
    """
    if not isinstance(exc, RateLimitError):
        return None
    if getattr(exc, "status_code", None) != 429:
        return None
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or code not in PROVIDER_SPEND_LIMIT_ERROR_CODES:
        return None
    return code

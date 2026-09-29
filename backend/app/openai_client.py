import os

from openai import OpenAI, OpenAIError


OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# Generic, key-free error text shared by every analysis route that needs
# OpenAI. Never include the key or any raw exception text in a response.
ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL = "Analysis service is not configured."


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

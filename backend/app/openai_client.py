import logging
import os

from openai import OpenAI, OpenAIError, RateLimitError

from .config import (
    DATABASE_URL_ENV,
    UPLOAD_DIR_ENV,
    database_is_default,
    upload_dir_is_default,
)
from .fake_analysis import FakeAnalysisClient


OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# Which analysis provider the analysis seams use. Unset or "openai": the
# real OpenAI client (unchanged behavior). "fake": the deterministic
# fake_analysis.FakeAnalysisClient for development and demos -- only when
# selected here, never as a fallback when OpenAI is missing or failing.
ANALYSIS_PROVIDER_ENV = "APPLY101_ANALYSIS_PROVIDER"
OPENAI_ANALYSIS_PROVIDER = "openai"
FAKE_ANALYSIS_PROVIDER = "fake"

logger = logging.getLogger(__name__)

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


class AnalysisProviderConfigError(ValueError):
    """APPLY101_ANALYSIS_PROVIDER has a value that cannot be used."""


def selected_analysis_provider() -> str:
    """Returns OPENAI_ANALYSIS_PROVIDER or FAKE_ANALYSIS_PROVIDER.

    The fake provider stores its results like real analyses (same model and
    prompt version, so matching accepts them), and its demo users' CVs are
    named after their database ids. So it is refused unless both
    APPLY101_DATABASE_URL and APPLY101_UPLOAD_DIR point away from the
    default (and the repository's real) database and upload directory.
    Any other value is an error, never silently the real provider.
    """
    value = (os.environ.get(ANALYSIS_PROVIDER_ENV) or "").strip().lower()
    if not value or value == OPENAI_ANALYSIS_PROVIDER:
        return OPENAI_ANALYSIS_PROVIDER
    if value == FAKE_ANALYSIS_PROVIDER:
        if database_is_default() or upload_dir_is_default():
            raise AnalysisProviderConfigError(
                f"{ANALYSIS_PROVIDER_ENV}={FAKE_ANALYSIS_PROVIDER} requires "
                f"{DATABASE_URL_ENV} and {UPLOAD_DIR_ENV} to point away from "
                "the default database and upload directory, so fake analyses "
                "and demo CVs never mix with real data."
            )
        return FAKE_ANALYSIS_PROVIDER
    raise AnalysisProviderConfigError(
        f"{ANALYSIS_PROVIDER_ENV} must be '{OPENAI_ANALYSIS_PROVIDER}' or "
        f"'{FAKE_ANALYSIS_PROVIDER}'; got {value!r}."
    )


def create_openai_client_or_none() -> OpenAI | FakeAnalysisClient | None:
    """Returns an OpenAI client, or None when analysis is not configured.

    With APPLY101_ANALYSIS_PROVIDER=fake it returns a FakeAnalysisClient
    instead and never constructs an OpenAI client, whatever OPENAI_API_KEY
    holds (see selected_analysis_provider).

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
    if selected_analysis_provider() == FAKE_ANALYSIS_PROVIDER:
        logger.warning(
            "Fake analysis provider active (%s=%s): analyses are deterministic "
            "demo data, not OpenAI results.",
            ANALYSIS_PROVIDER_ENV,
            FAKE_ANALYSIS_PROVIDER,
        )
        return FakeAnalysisClient()
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

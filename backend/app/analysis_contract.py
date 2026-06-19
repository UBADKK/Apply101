"""Versioned contracts shared by analysis and matching stages.

Bump the related version whenever a prompt, schema, taxonomy, normalization rule,
or scoring/eligibility behavior changes in a way that can affect stored results.
"""

PROFILE_ANALYSIS_MODEL = "gpt-4.1-mini"
PROFILE_ANALYSIS_PROMPT_VERSION = "profile_analysis_v5"

JOB_ANALYSIS_MODEL = "gpt-4.1-mini"
JOB_ANALYSIS_PROMPT_VERSION = "job_analysis_v9"

MATCH_MODEL = "backend_rule_based"
MATCH_VERSION = "backend_match_v4"

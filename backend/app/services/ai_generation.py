"""
AI Schema Generation Module (Module 3).

Responsible for ONE thing: turning natural language requirements into a
CandidateSchema. It does not validate correctness — that is deliberately
left to the structural validation (Module 4) and normalization validation
(Module 5) modules, which are independent of this one. This module also
exposes `generate_correction`, used by the bounded correction loop (Module 6)
to re-prompt the LLM with specific, structured feedback.

Keeping this module "dumb" (generate only, no self-checking) is intentional:
it is what keeps the system's correctness guarantee independent of the LLM's
own judgment, per the project's core architectural claim.

Uses Groq's API (OpenAI-compatible), which hosts open-source models like
Llama 3.3 70B with a generous free tier — the same provider used elsewhere
in this project's companion (video conferencing) app.
"""

from __future__ import annotations
import json
import logging
import re
import time
from typing import Optional

from groq import Groq, RateLimitError

from app.config import settings
from app.models.schema_models import CandidateSchema
from app.services.prompts import (
    SYSTEM_PROMPT,
    build_generation_prompt,
    build_correction_prompt,
    build_self_critique_prompt,
)

logger = logging.getLogger(__name__)


class SchemaGenerationError(Exception):
    """Raised when the LLM output cannot be parsed into a valid CandidateSchema."""


_RETRY_AFTER_PATTERN = re.compile(
    r"try again in (?:(\d+)m)?([\d.]+)(ms|s)\b", re.IGNORECASE
)


def _parse_retry_after_seconds(message: str) -> Optional[float]:
    """Groq's 429 error messages include a suggested wait time. Two formats
    are seen in practice:
      - short (per-minute) limits: "Please try again in 592.5ms" or "1.1s"
      - longer (per-day) limits:   "Please try again in 9m55.728s"
    The second form combines a whole-minutes prefix with a seconds
    remainder; a pattern that only looks for a single numeric+unit pair
    silently fails to match it at all (returns None, falling back to a
    much-too-short default backoff) rather than mis-parsing it — which is
    just as dangerous for a rate limit that's actually ~10 minutes long,
    since the caller would retry 5 times in under a minute and still fail.
    """
    match = _RETRY_AFTER_PATTERN.search(message)
    if not match:
        return None
    minutes_str, value_str, unit = match.groups()
    seconds = float(value_str)
    if unit.lower() == "ms":
        seconds /= 1000.0
    minutes = float(minutes_str) if minutes_str else 0.0
    return minutes * 60.0 + seconds


def _extract_json(raw_text: str) -> str:
    """
    Defensive cleanup: some models wrap JSON in ```json ... ``` fences even
    when told not to. Strip those if present so json.loads doesn't choke.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop the first fence line (``` or ```json) and the trailing fence
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _call_llm(client: Groq, user_prompt: str, max_retries: int = 5) -> str:
    """
    Calls the LLM with retry-with-backoff on rate limiting (HTTP 429).
    Free/on-demand tiers have low tokens-per-minute limits, and a batch
    experiment run (many calls back to back) will hit this routinely —
    so this is not an edge case to ignore, it's an expected condition to
    handle gracefully rather than letting it crash an entire experiment
    run after only a few domains have completed.
    """
    last_error: Optional[RateLimitError] = None
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=settings.model_name,
                max_tokens=settings.max_tokens,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content
        except RateLimitError as e:
            last_error = e
            suggested = _parse_retry_after_seconds(str(e))
            # Add a small buffer on top of the API's own suggested wait,
            # and fall back to exponential backoff if no hint was given.
            wait = (suggested + 0.5) if suggested is not None else (2.0 * (2 ** attempt))
            logger.warning(
                "Rate limited (attempt %d/%d), waiting %.1fs before retrying",
                attempt + 1, max_retries, wait,
            )
            time.sleep(wait)

    raise SchemaGenerationError(
        f"Rate limit persisted after {max_retries} retries: {last_error}"
    )


def _parse_schema(raw_text: str) -> CandidateSchema:
    cleaned = _extract_json(raw_text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise SchemaGenerationError(f"Model output was not valid JSON: {e}\n---\n{raw_text[:500]}")

    try:
        return CandidateSchema(**data)
    except Exception as e:  # pydantic ValidationError, KeyError, etc.
        raise SchemaGenerationError(f"Model output did not match the expected schema shape: {e}")


class SchemaGenerator:
    def __init__(self, client: Optional[Groq] = None):
        self.client = client or Groq(api_key=settings.groq_api_key)

    def generate(self, requirements: str) -> tuple[CandidateSchema, str]:
        """
        Generate a candidate schema from natural language requirements.
        Returns (parsed_schema, raw_model_output) — the raw output is kept
        so it can be logged/inspected during development and reused as
        `previous_schema_json` if a correction round is needed.
        """
        prompt = build_generation_prompt(requirements)
        raw = _call_llm(self.client, prompt)
        schema = _parse_schema(raw)
        logger.info("Generated schema with %d table(s)", len(schema.tables))
        return schema, raw

    def generate_correction(
        self, requirements: str, previous_schema_json: str, issues: list[str]
    ) -> tuple[CandidateSchema, str]:
        """
        Used by the correction loop (Module 6). Re-prompts the LLM with the
        specific validation issues found in the previous attempt.
        """
        prompt = build_correction_prompt(requirements, previous_schema_json, issues)
        raw = _call_llm(self.client, prompt)
        schema = _parse_schema(raw)
        logger.info("Correction produced schema with %d table(s)", len(schema.tables))
        return schema, raw

    def generate_self_critique(
        self, requirements: str, schema_json: str
    ) -> tuple[CandidateSchema, str]:
        """
        Comparison condition C (for the paper's evaluation only — never
        used by the actual correction loop). Asks the LLM to review its
        own output for normalization violations with NO hint about what,
        if anything, is wrong. This is what makes it a fair test of
        self-critique rather than a repeat of the guided correction path.
        """
        prompt = build_self_critique_prompt(requirements, schema_json)
        raw = _call_llm(self.client, prompt)
        schema = _parse_schema(raw)
        logger.info("Self-critique produced schema with %d table(s)", len(schema.tables))
        return schema, raw

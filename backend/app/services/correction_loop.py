"""
Bounded Correction Loop (Module 6).

This is where the project's core architectural claim actually gets
exercised end to end: AI proposes (Module 3), the deterministic engines
verify (Modules 4 + 5), and — only if verification fails — the AI gets a
second (third, fourth) chance, fed the *specific* violations found, not a
vague "try again."

Design notes that matter for the paper:

- Attempt 0 is always the raw, unvalidated first-pass generation. This is
  the "baseline" the methodology measures against — nothing here special-
  cases or pre-filters it.
- The loop is bounded by `max_correction_attempts` (default 3, from
  config). It never loops indefinitely chasing convergence.
- If correction fails to converge within the bound, this module does NOT
  raise or hide that fact — it returns a result whose `converged` flag is
  False and whose final attempt still carries whatever issues remain. The
  paper's methodology explicitly calls for reporting non-convergence
  honestly, so silently "succeeding" here would misrepresent the data.
- Validation itself (structural + normalization) is completely decoupled
  from the LLM calls in this file — this module only orchestrates calls
  into Modules 4 and 5, it does not re-implement or duplicate any check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import settings
from app.models.schema_models import CandidateSchema
from app.services.ai_generation import SchemaGenerator, SchemaGenerationError
from app.services.normalization import validate_normalization
from app.services.structural import validate_structure

logger = logging.getLogger(__name__)


@dataclass
class AttemptRecord:
    """One pass through generate-or-correct + validate.

    attempt_number: 0 for the initial, unvalidated generation; 1..N for
    each subsequent correction round.
    """
    attempt_number: int
    schema: CandidateSchema
    raw_model_output: str
    structural_issues: list[str]
    normalization_issues: list[str]
    generation_failed: bool = False

    @property
    def all_issues(self) -> list[str]:
        return self.structural_issues + self.normalization_issues

    @property
    def issue_count(self) -> int:
        return len(self.all_issues)

    @property
    def is_clean(self) -> bool:
        return self.issue_count == 0


@dataclass
class CorrectionLoopResult:
    """The full record of one requirements string's run through the
    generate -> validate -> correct loop. Carries everything the paper's
    methodology needs (baseline count, final count, convergence, rounds
    used) without forcing the caller to re-derive it from individual
    attempts.
    """
    requirements: str
    attempts: list[AttemptRecord] = field(default_factory=list)

    @property
    def final_attempt(self) -> AttemptRecord:
        return self.attempts[-1]

    @property
    def final_schema(self) -> CandidateSchema:
        return self.final_attempt.schema

    @property
    def converged(self) -> bool:
        return self.final_attempt.is_clean

    @property
    def baseline_issue_count(self) -> int:
        """Issues found in the raw, unvalidated attempt-0 generation."""
        return self.attempts[0].issue_count

    @property
    def final_issue_count(self) -> int:
        return self.final_attempt.issue_count

    @property
    def correction_rounds_used(self) -> int:
        """Number of correction attempts actually made, excluding attempt 0."""
        return len(self.attempts) - 1


def _validate(schema: CandidateSchema) -> tuple[list[str], list[str]]:
    """Runs Module 4 (structural) and Module 5 (normalization) — the two
    independent, deterministic checks — against one candidate schema."""
    return validate_structure(schema), validate_normalization(schema)


def run_correction_loop(
    requirements: str,
    generator: SchemaGenerator | None = None,
    max_attempts: int | None = None,
) -> CorrectionLoopResult:
    """
    Runs the full pipeline: generate -> validate -> (correct -> validate)*.

    Stops as soon as an attempt validates clean, or once `max_attempts`
    correction rounds have been used — whichever comes first. Accepts an
    injected `generator` (anything exposing `.generate()` and
    `.generate_correction()` with the same signatures as SchemaGenerator)
    so this can be unit-tested without hitting the real LLM API.
    """
    generator = generator or SchemaGenerator()
    max_attempts = (
        max_attempts if max_attempts is not None else settings.max_correction_attempts
    )

    result = CorrectionLoopResult(requirements=requirements)

    # --- Attempt 0: initial, unvalidated generation (the baseline) ---
    schema, raw = generator.generate(requirements)
    structural_issues, normalization_issues = _validate(schema)
    result.attempts.append(
        AttemptRecord(0, schema, raw, structural_issues, normalization_issues)
    )
    logger.info("Attempt 0: %d issue(s) found", result.attempts[0].issue_count)

    # --- Correction rounds, bounded by max_attempts ---
    attempt_number = 0
    while not result.attempts[-1].is_clean and attempt_number < max_attempts:
        attempt_number += 1
        prev = result.attempts[-1]

        try:
            schema, raw = generator.generate_correction(
                requirements, prev.raw_model_output, prev.all_issues
            )
        except SchemaGenerationError as e:
            # The correction call itself came back unparsable. Record this
            # as a terminal, failed attempt (still carrying the previous
            # issues, since nothing improved) and stop — we don't retry a
            # broken correction call indefinitely, and we don't silently
            # fall back to a stale "success."
            logger.warning("Correction attempt %d failed to parse: %s", attempt_number, e)
            result.attempts.append(
                AttemptRecord(
                    attempt_number,
                    prev.schema,
                    prev.raw_model_output,
                    prev.structural_issues,
                    prev.normalization_issues,
                    generation_failed=True,
                )
            )
            break

        structural_issues, normalization_issues = _validate(schema)
        result.attempts.append(
            AttemptRecord(attempt_number, schema, raw, structural_issues, normalization_issues)
        )
        logger.info(
            "Attempt %d: %d issue(s) found (was %d)",
            attempt_number, result.attempts[-1].issue_count, prev.issue_count,
        )

    # Annotate the final schema's own bookkeeping fields (these already
    # exist on CandidateSchema for exactly this purpose) so any downstream
    # consumer — the API response, the deployment module — can inspect
    # the outcome without needing the full CorrectionLoopResult object.
    final = result.attempts[-1]
    final.schema.structural_issues = final.structural_issues
    final.schema.normalization_violations = final.normalization_issues
    final.schema.correction_attempt = final.attempt_number

    return result

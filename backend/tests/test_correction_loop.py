"""
Tests for the bounded correction loop (Module 6).

Uses a FakeGenerator (no real LLM calls, no API key needed) that returns a
pre-scripted sequence of schemas, so these tests are fast, deterministic,
and exercise the loop's control flow in isolation from Groq/network
reliability — exactly the kind of "logical, traceable" testing the
normalization engine itself uses.
"""

import pytest

from app.models.schema_models import CandidateSchema, TableDef, ColumnDef, FunctionalDependency
from app.services.ai_generation import SchemaGenerationError
from app.services.correction_loop import run_correction_loop


def _col(name, dtype="INTEGER", pk=False, fk=None, nullable=True):
    return ColumnDef(name=name, data_type=dtype, nullable=nullable, primary_key=pk, foreign_key=fk)


def _fd(table, det, dep):
    return FunctionalDependency(table=table, determinant=det, dependent=dep)


def _clean_schema():
    """A minimal, structurally sound, fully-normalized schema."""
    return CandidateSchema(
        tables=[
            TableDef(name="students", columns=[_col("student_id", pk=True), _col("name", "VARCHAR(100)")],
                     primary_key=["student_id"]),
        ],
        functional_dependencies=[_fd("students", ["student_id"], ["name"])],
    )


def _violating_schema():
    """3NF violation: agent_name -> branch_office, PK = property_id."""
    return CandidateSchema(
        tables=[
            TableDef(name="listings",
                     columns=[_col("property_id", pk=True), _col("agent_name", "VARCHAR(100)"),
                              _col("branch_office", "VARCHAR(100)")],
                     primary_key=["property_id"]),
        ],
        functional_dependencies=[
            _fd("listings", ["property_id"], ["agent_name", "branch_office"]),
            _fd("listings", ["agent_name"], ["branch_office"]),
        ],
    )


class FakeGenerator:
    """
    Stands in for SchemaGenerator. `responses` is a list of items, consumed
    in order: the first is returned by `.generate()`, each subsequent one
    by successive calls to `.generate_correction()`. An item that is the
    string "FAIL" causes that call to raise SchemaGenerationError instead.
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    def _next(self):
        response = self._responses[self.calls]
        self.calls += 1
        if response == "FAIL":
            raise SchemaGenerationError("simulated unparsable model output")
        return response, f"raw_output_{self.calls - 1}"

    def generate(self, requirements: str):
        return self._next()

    def generate_correction(self, requirements: str, previous_schema_json: str, issues: list[str]):
        return self._next()


# ---------------------------------------------------------------------

def test_converges_immediately_when_first_attempt_is_clean():
    gen = FakeGenerator([_clean_schema()])
    result = run_correction_loop("some requirements", generator=gen, max_attempts=3)

    assert result.converged is True
    assert len(result.attempts) == 1
    assert result.baseline_issue_count == 0
    assert result.final_issue_count == 0
    assert result.correction_rounds_used == 0
    assert gen.calls == 1  # only the initial generate() call, no corrections needed


def test_converges_after_one_correction_round():
    gen = FakeGenerator([_violating_schema(), _clean_schema()])
    result = run_correction_loop("some requirements", generator=gen, max_attempts=3)

    assert result.converged is True
    assert len(result.attempts) == 2
    assert result.baseline_issue_count > 0  # attempt 0 had real violations
    assert result.final_issue_count == 0
    assert result.correction_rounds_used == 1
    assert gen.calls == 2


def test_never_converges_stops_at_max_attempts():
    # Every attempt (initial + 3 corrections) comes back violating.
    gen = FakeGenerator([_violating_schema()] * 4)
    result = run_correction_loop("some requirements", generator=gen, max_attempts=3)

    assert result.converged is False
    assert len(result.attempts) == 4  # attempt 0 + 3 correction rounds
    assert result.correction_rounds_used == 3
    assert result.final_issue_count > 0
    assert gen.calls == 4  # loop stopped, did not exceed the bound


def test_correction_call_failure_terminates_loop_without_crashing():
    # Initial attempt violates; the one correction attempt is unparsable.
    gen = FakeGenerator([_violating_schema(), "FAIL"])
    result = run_correction_loop("some requirements", generator=gen, max_attempts=3)

    assert result.converged is False
    assert len(result.attempts) == 2
    assert result.attempts[-1].generation_failed is True
    # The failed attempt should carry forward the previous attempt's
    # issues rather than silently reporting zero issues.
    assert result.final_issue_count == result.attempts[0].issue_count


def test_final_schema_bookkeeping_fields_are_populated():
    gen = FakeGenerator([_violating_schema(), _clean_schema()])
    result = run_correction_loop("some requirements", generator=gen, max_attempts=3)

    final = result.final_schema
    assert final.structural_issues == []
    assert final.normalization_violations == []
    assert final.correction_attempt == 1


def test_respects_custom_max_attempts_bound():
    gen = FakeGenerator([_violating_schema()] * 10)
    result = run_correction_loop("some requirements", generator=gen, max_attempts=1)

    assert len(result.attempts) == 2  # attempt 0 + exactly 1 correction round
    assert result.correction_rounds_used == 1
    assert gen.calls == 2

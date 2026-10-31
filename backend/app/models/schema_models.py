"""
Shared JSON schema data model (Module 2).

This is the single source of truth that every module in the pipeline reads
and writes: the AI generation module produces a CandidateSchema, the
structural validator and normalization engine both read/annotate it, and
the correction loop feeds violations back to the AI generation module.

Keeping this as one well-typed Pydantic model (instead of loosely-typed
dicts) means every downstream module can trust the shape of the data it
receives, and FastAPI gets automatic request/response validation for free.
"""

from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class ColumnDef(BaseModel):
    """A single column in a table."""
    name: str
    data_type: str = Field(..., description="e.g. INTEGER, VARCHAR(100), BOOLEAN, DATE")
    nullable: bool = True
    primary_key: bool = False
    # "other_table.column" if this column is a foreign key, else None
    foreign_key: Optional[str] = None


class TableDef(BaseModel):
    """A single table, with its own primary key (supports composite keys)."""
    name: str
    columns: List[ColumnDef]
    # List of column names forming the primary key. For a single-column key,
    # this will usually just duplicate what's marked primary_key=True above;
    # composite keys (e.g. ["student_id", "course_id"]) are only expressible here.
    primary_key: List[str] = Field(default_factory=list)


class FunctionalDependency(BaseModel):
    """
    A functional dependency of the form determinant -> dependent, scoped to
    one table. E.g. determinant=["student_id"], dependent=["student_name"]
    means "student_id determines student_name" within that table.

    These are inferred by the AI generation module from the natural language
    requirements, and are what the normalization validation engine (Module 5)
    uses to compute closures, candidate keys, and 1NF/2NF/3NF violations.
    """
    table: str
    determinant: List[str]
    dependent: List[str]


class CandidateSchema(BaseModel):
    """
    The full candidate schema produced by the AI generation module, and the
    object passed through structural validation, normalization validation,
    and the correction loop.
    """
    tables: List[TableDef]
    functional_dependencies: List[FunctionalDependency] = Field(default_factory=list)

    # Populated by later modules, not by the AI generation step itself.
    # Kept here so the same object can carry validation results through the
    # pipeline without inventing a second data structure.
    structural_issues: List[str] = Field(default_factory=list)
    normalization_violations: List[str] = Field(default_factory=list)
    correction_attempt: int = 0


class GenerateRequest(BaseModel):
    """Request body for POST /api/generate."""
    requirements: str = Field(..., min_length=5, description="Plain-English description of the application")


class GenerateResponse(BaseModel):
    """Response body for POST /api/generate."""
    schema_: CandidateSchema = Field(..., alias="schema")
    raw_model_output: Optional[str] = Field(
        default=None,
        description="Unparsed LLM output, kept for debugging prompt/parsing issues during development",
    )

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------
# API models for the validation / correction / export endpoints
# ---------------------------------------------------------------------

class ValidateResponse(BaseModel):
    """Response body for POST /api/validate."""
    valid: bool
    structural_issues: List[str]
    normalization_issues: List[str]


class DDLResponse(BaseModel):
    """Response body for POST /api/ddl."""
    ddl: str


class AttemptSummary(BaseModel):
    """One generate-or-correct attempt in the bounded correction loop."""
    attempt_number: int = Field(..., description="0 = initial unvalidated generation, 1..N = correction rounds")
    issue_count: int
    structural_issues: List[str]
    normalization_issues: List[str]
    generation_failed: bool = False


class DesignResponse(BaseModel):
    """
    Response body for POST /api/design (the full generate -> validate ->
    correct pipeline). `ddl` is only populated when the loop converged;
    a schema that still has issues is returned for inspection but is never
    offered as ready-to-run SQL.
    """
    converged: bool
    baseline_issue_count: int
    final_issue_count: int
    correction_rounds_used: int
    attempts: List[AttemptSummary]
    schema_: CandidateSchema = Field(..., alias="schema")
    ddl: Optional[str] = None
    ddl_error: Optional[str] = Field(
        default=None,
        description="Why DDL could not be produced for a schema that passed both validators",
    )

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------
# Live deployment models (POST /api/deploy)
# ---------------------------------------------------------------------

class DeployedColumn(BaseModel):
    """A column as PostgreSQL actually created it (read back from the catalog)."""
    name: str
    type: str
    nullable: bool


class DeployedTable(BaseModel):
    name: str
    columns: List[DeployedColumn]
    constraints: List[str] = Field(
        default_factory=list,
        description="Primary and foreign key constraints, as PostgreSQL reports them",
    )


class DeployResponse(BaseModel):
    """
    Outcome of running a schema's DDL against a real PostgreSQL database.
    `deployed=False` carries PostgreSQL's own error message in `error`.
    A dry run (keep=False) reports what WOULD exist but leaves nothing behind,
    so `kept` is False and `schema_name` is None.
    """
    deployed: bool
    kept: bool = False
    schema_name: Optional[str] = Field(
        default=None, description="Present only when keep=true; pass it to DELETE /api/deploy/{schema_name}"
    )
    tables: List[DeployedTable] = Field(default_factory=list)
    ddl: str
    error: Optional[str] = None

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from groq import APIError

from app.config import settings
from app.models.schema_models import (
    AttemptSummary,
    CandidateSchema,
    DDLResponse,
    DeployResponse,
    DesignResponse,
    GenerateRequest,
    GenerateResponse,
    ValidateResponse,
)
from app.services.ai_generation import SchemaGenerator, SchemaGenerationError
from app.services.correction_loop import run_correction_loop
from app.services.ddl import DDLGenerationError, generate_ddl
from app.services.deploy import DeploymentUnavailable, InvalidSandboxName, SandboxDeployer
from app.services.normalization import validate_normalization
from app.services.structural import validate_structure

router = APIRouter(prefix="/api", tags=["schema-design"])


@lru_cache
def get_generator() -> SchemaGenerator:
    """Created lazily (not at import time) so the app can be imported, and
    the non-LLM endpoints used, without a GROQ_API_KEY configured. Exposed as
    a FastAPI dependency so tests can substitute a fake generator."""
    return SchemaGenerator()


def get_deployer() -> SandboxDeployer:
    """Dependency: a deployer bound to DATABASE_URL. Raises 503 if it is not
    configured, so every other endpoint keeps working without a database."""
    if not settings.database_url:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not configured. Add it to backend/.env, "
                   "e.g. DATABASE_URL=postgresql://postgres:password@localhost:5432/postgres",
        )
    return SandboxDeployer(settings.database_url)


@router.post("/generate", response_model=GenerateResponse, response_model_by_alias=True)
def generate_schema(
    request: GenerateRequest, generator: SchemaGenerator = Depends(get_generator)
) -> GenerateResponse:
    """
    Module 3 only: natural language requirements in, candidate schema out.
    No validation happens here on purpose; see /api/design for the full
    generate -> validate -> correct pipeline.
    """
    try:
        schema, raw = generator.generate(request.requirements)
    except SchemaGenerationError as e:
        # 502: the LLM (an upstream dependency) returned something we
        # couldn't parse, or stayed rate limited. Not a client error.
        raise HTTPException(status_code=502, detail=str(e))
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"LLM provider error: {e}")

    return GenerateResponse(schema=schema, raw_model_output=raw)


@router.post("/validate", response_model=ValidateResponse)
def validate_schema(schema: CandidateSchema) -> ValidateResponse:
    """Run the structural and normalization validators on a caller-supplied
    schema. No LLM involved; purely the deterministic verifier."""
    structural = validate_structure(schema)
    normalization = validate_normalization(schema)
    return ValidateResponse(
        valid=not structural and not normalization,
        structural_issues=structural,
        normalization_issues=normalization,
    )


@router.post("/ddl", response_model=DDLResponse)
def export_ddl(schema: CandidateSchema) -> DDLResponse:
    """Export a schema as PostgreSQL DDL. No LLM involved."""
    try:
        return DDLResponse(ddl=generate_ddl(schema))
    except DDLGenerationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/design", response_model=DesignResponse, response_model_by_alias=True)
def design_schema(
    request: GenerateRequest, generator: SchemaGenerator = Depends(get_generator)
) -> DesignResponse:
    """
    The full pipeline: generate a candidate schema, validate it
    (structural + normalization), and run the bounded correction loop. The
    response reports every attempt, including a failure to converge, rather
    than hiding it. DDL is included only for a fully clean schema.
    """
    try:
        result = run_correction_loop(request.requirements, generator=generator)
    except SchemaGenerationError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"LLM provider error: {e}")

    ddl = None
    ddl_error = None
    if result.converged:
        try:
            ddl = generate_ddl(result.final_schema)
        except DDLGenerationError as e:
            # Passed both validators but still can't be expressed as safe SQL
            # (e.g. an unsupported column type). Report as not converged,
            # with the reason, rather than shipping a schema we can't deploy.
            ddl_error = str(e)

    return DesignResponse(
        converged=result.converged and ddl is not None,
        baseline_issue_count=result.baseline_issue_count,
        final_issue_count=result.final_issue_count,
        correction_rounds_used=result.correction_rounds_used,
        attempts=[
            AttemptSummary(
                attempt_number=a.attempt_number,
                issue_count=a.issue_count,
                structural_issues=a.structural_issues,
                normalization_issues=a.normalization_issues,
                generation_failed=a.generation_failed,
            )
            for a in result.attempts
        ],
        schema=result.final_schema,
        ddl=ddl,
        ddl_error=ddl_error,
    )


@router.post("/deploy", response_model=DeployResponse)
def deploy_schema(
    schema: CandidateSchema, keep: bool = False, deployer: SandboxDeployer = Depends(get_deployer)
) -> DeployResponse:
    """
    Run a validated schema's DDL against the real PostgreSQL database in an
    isolated sandbox schema and report what was created, or PostgreSQL's own
    error. keep=false (default) is a dry run that leaves nothing behind;
    keep=true commits and returns a schema_name you can later remove with
    DELETE /api/deploy/{schema_name}.

    Only schemas that pass both validators are deployed (409 otherwise), and
    only DDL generated by this project is executed, never client-supplied SQL.
    """
    structural = validate_structure(schema)
    normalization = validate_normalization(schema)
    if structural or normalization:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Schema has validation issues; fix them before deploying.",
                "structural_issues": structural,
                "normalization_issues": normalization,
            },
        )
    try:
        ddl = generate_ddl(schema)
    except DDLGenerationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        return deployer.deploy(ddl, keep=keep)
    except DeploymentUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.delete("/deploy/{schema_name}")
def teardown_deployment(schema_name: str, deployer: SandboxDeployer = Depends(get_deployer)) -> dict:
    """Remove a sandbox created with keep=true. Only sandbox_* names are accepted."""
    try:
        return {"dropped": deployer.teardown(schema_name)}
    except InvalidSandboxName as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DeploymentUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))

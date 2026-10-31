"""
Prompt templates for the AI schema generation module (Module 3).

Kept separate from ai_generation.py so prompt wording can be iterated on
independently of the calling/parsing logic — this is the part you'll likely
tune the most while testing generation quality across different domains.
"""

SYSTEM_PROMPT = """You are a database design assistant. Given a natural language \
description of an application, produce a candidate relational database schema.

You must respond with ONLY a single JSON object, no prose before or after it, \
no markdown code fences. The JSON must conform exactly to this structure:

{
  "tables": [
    {
      "name": "string (snake_case, plural, e.g. 'students')",
      "columns": [
        {
          "name": "string (snake_case)",
          "data_type": "string (e.g. INTEGER, VARCHAR(100), BOOLEAN, DATE, DECIMAL(10,2))",
          "nullable": true,
          "primary_key": false,
          "foreign_key": null
        }
      ],
      "primary_key": ["column_name", "..."]
    }
  ],
  "functional_dependencies": [
    {
      "table": "table_name",
      "determinant": ["column_name", "..."],
      "dependent": ["column_name", "..."]
    }
  ]
}

Rules:
- Every table must have a primary_key (composite keys are allowed and expected
  for tables that represent many-to-many relationships, e.g. enrollments).
- foreign_key values must be formatted as "other_table_name.column_name".
- List every functional dependency you can reasonably infer for each table,
  including the trivial one where the primary key determines every other
  column in that table, AND any dependency where a NON-key column determines
  another non-key column (this is what lets a downstream validator detect
  normalization issues, so do not skip these even if they reveal that your
  own design may need a second table).
- Do not add columns, tables, or relationships that are not implied by the
  requirements. Do not explain your reasoning outside the JSON.
"""

GENERATION_USER_PROMPT_TEMPLATE = """Application requirements:
\"\"\"{requirements}\"\"\"

Produce the JSON schema now."""


CORRECTION_USER_PROMPT_TEMPLATE = """Your previous schema for these requirements had validation issues.

Original requirements:
\"\"\"{requirements}\"\"\"

Your previous JSON schema:
{previous_schema_json}

Validation issues found (fix ALL of these):
{issues_list}

How to fix normalization issues (1NF/2NF/3NF/BCNF violations):
- The correct fix is DECOMPOSITION: move the violating determinant and its
  dependent attribute(s) into their own new table (e.g. a "therapist_id ->
  clinic_branch_id" violation means creating a "therapists" table with
  therapist_id as its primary key and clinic_branch_id as one of its
  columns), then replace the dependent attribute(s) in the original table
  with a foreign key reference to the new table's primary key.
- Do NOT fix a normalization violation by simply deleting the dependent
  column, deleting the determinant column, or deleting the functional
  dependency declaration. Every piece of information present in the
  original requirements ("{requirements}") must still be represented
  SOMEWHERE in the corrected schema, even if it moves to a different
  table. Silently dropping a column that was explicitly requested is
  never an acceptable fix, even if it makes the normalization checker
  pass.
- If a structural issue says a foreign_key references a table that does
  not exist, the fix is almost always to ADD that missing table (with a
  sensible primary key and any columns implied by the requirements), not
  to remove the foreign_key or the column referencing it.

Produce a corrected JSON schema now, following the exact same structure as before. \
Respond with ONLY the corrected JSON object, no prose."""


def build_generation_prompt(requirements: str) -> str:
    return GENERATION_USER_PROMPT_TEMPLATE.format(requirements=requirements.strip())


def build_correction_prompt(requirements: str, previous_schema_json: str, issues: list[str]) -> str:
    issues_list = "\n".join(f"- {issue}" for issue in issues)
    return CORRECTION_USER_PROMPT_TEMPLATE.format(
        requirements=requirements.strip(),
        previous_schema_json=previous_schema_json,
        issues_list=issues_list,
    )


# ---------------------------------------------------------------------
# LLM self-critique prompt (comparison condition C).
#
# Deliberately does NOT tell the model what, if anything, is wrong — that
# is the entire point of this condition. It exists only to measure whether
# an LLM reviewing its own output catches the same violations an
# independent, deterministic verifier catches, without being handed the
# answer. Feeding it the specific issue list here would defeat the
# experiment; that is why this prompt is a separate template from
# CORRECTION_USER_PROMPT_TEMPLATE rather than a variant of it.
# ---------------------------------------------------------------------

SELF_CRITIQUE_USER_PROMPT_TEMPLATE = """Here is a database schema you (or a system like you) previously \
generated for the following application requirements.

Original requirements:
\"\"\"{requirements}\"\"\"

Schema to review:
{schema_json}

Carefully review this schema against relational database normalization theory \
(1NF, 2NF, 3NF, and BCNF). Check every functional dependency for partial \
dependencies (2NF), transitive dependencies (3NF), and non-superkey \
determinants (BCNF), and check every column for atomicity (1NF).

If you find any normalization violation, fix it via decomposition (move the \
violating attribute(s) into a new table, connected by a foreign key), without \
deleting any information that was present in the original requirements. If you \
find no violations, return the schema exactly as given.

Respond with ONLY the JSON schema (corrected if you found issues, unchanged if \
you did not), following the exact same structure as before. No prose, no \
explanation of what you found or changed."""


def build_self_critique_prompt(requirements: str, schema_json: str) -> str:
    return SELF_CRITIQUE_USER_PROMPT_TEMPLATE.format(
        requirements=requirements.strip(),
        schema_json=schema_json,
    )

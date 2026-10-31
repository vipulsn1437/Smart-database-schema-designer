"""
Structural Validation Module (Module 4).

Checks a CandidateSchema for issues that are independent of functional
dependencies: duplicate table/column names, tables missing a primary key,
and foreign keys that reference non-existent tables or columns.

This module deliberately requires no functional dependency information,
so it can be implemented, tested, and validated before the normalization
engine (Module 5), which does depend on functional dependencies.
"""

from __future__ import annotations
from app.models.schema_models import CandidateSchema


def validate_structure(schema: CandidateSchema) -> list[str]:
    """
    Returns a list of human-readable structural issue descriptions.
    An empty list means the schema is structurally sound.
    """
    issues: list[str] = []

    # --- Duplicate table names ---
    table_names = [t.name for t in schema.tables]
    seen_tables = set()
    for name in table_names:
        if name in seen_tables:
            issues.append(f"Duplicate table name: '{name}' appears more than once.")
        seen_tables.add(name)

    # Build a lookup of table -> set of column names, for FK validation later.
    # Use the first occurrence of a duplicated table for this purpose.
    table_columns: dict[str, set[str]] = {}

    for table in schema.tables:
        col_names = [c.name for c in table.columns]

        # --- Duplicate column names within a table ---
        seen_cols = set()
        for col_name in col_names:
            if col_name in seen_cols:
                issues.append(
                    f"Duplicate column name: '{table.name}.{col_name}' appears more than once."
                )
            seen_cols.add(col_name)

        table_columns[table.name] = set(col_names)

        # --- Missing primary key ---
        if not table.primary_key:
            issues.append(f"Table '{table.name}' has no declared primary key.")
        else:
            # Every declared PK column must actually exist on the table.
            for pk_col in table.primary_key:
                if pk_col not in seen_cols:
                    issues.append(
                        f"Table '{table.name}' declares primary key column "
                        f"'{pk_col}' which does not exist in its columns."
                    )

    # --- Orphaned foreign key references ---
    for table in schema.tables:
        for col in table.columns:
            if col.foreign_key is None:
                continue
            if "." not in col.foreign_key:
                issues.append(
                    f"Column '{table.name}.{col.name}' has a malformed foreign_key "
                    f"value '{col.foreign_key}' (expected 'table.column')."
                )
                continue

            ref_table, ref_column = col.foreign_key.split(".", 1)

            if ref_table not in table_columns:
                issues.append(
                    f"Column '{table.name}.{col.name}' references table "
                    f"'{ref_table}', which does not exist in the schema."
                )
                continue

            if ref_column not in table_columns[ref_table]:
                issues.append(
                    f"Column '{table.name}.{col.name}' references "
                    f"'{ref_table}.{ref_column}', which does not exist."
                )

    return issues

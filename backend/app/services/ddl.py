"""
SQL DDL generation (PostgreSQL).

Turns a CandidateSchema into ready-to-run CREATE TABLE statements. This is
both the "SQL DDL export" feature and the input to the live-deployment step.

Because this output will eventually be EXECUTED against a real database, and
the schema it is built from originates from LLM output, this module treats
every string it interpolates as untrusted:

  - identifiers (table/column/constraint names) are validated and, unless
    they are plain lowercase names that are not reserved words, quoted with
    embedded double quotes escaped
  - data types are checked against a strict allow-pattern (a type name plus
    optional numeric precision). A "type" such as
    `INTEGER); DROP TABLE users; --` is rejected, not passed through.

Anything the module cannot emit safely raises DDLGenerationError instead of
producing SQL that might do something other than create the schema.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from app.models.schema_models import CandidateSchema, TableDef


class DDLGenerationError(ValueError):
    """Raised when a schema cannot be converted to safe, valid DDL."""


# PostgreSQL's fully reserved words (these must be quoted when used as names).
_RESERVED = frozenset("""
all analyse analyze and any array as asc asymmetric authorization binary both
case cast check collate collation column concurrently constraint create cross
current_catalog current_date current_role current_schema current_time
current_timestamp current_user default deferrable desc distinct do else end
except false fetch for foreign freeze from full grant group having ilike in
initially inner intersect into is isnull join lateral leading left like limit
localtime localtimestamp natural not notnull null offset on only or order outer
overlaps placing primary references returning right select session_user similar
some symmetric table tablesample then to trailing true union unique user using
variadic verbose when where window with
""".split())

_SIMPLE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_MAX_IDENT_LEN = 63  # PostgreSQL NAMEDATALEN - 1

# type name (letters/spaces) + optional (n) or (n,m)
_TYPE_PATTERN = re.compile(r"^([A-Za-z][A-Za-z ]*?)\s*(\(\s*\d+\s*(?:,\s*\d+\s*)?\))?$")

# Common non-PostgreSQL type names LLMs emit -> PostgreSQL equivalent.
# value = (postgres_name, keep_parameters)
_TYPE_MAP: Dict[str, Tuple[str, bool]] = {
    "DATETIME": ("TIMESTAMP", True),
    "TINYINT": ("SMALLINT", False),
    "MEDIUMINT": ("INTEGER", False),
    "DOUBLE": ("DOUBLE PRECISION", False),
    "STRING": ("TEXT", False),
    "LONGTEXT": ("TEXT", False),
    "MEDIUMTEXT": ("TEXT", False),
    "TINYTEXT": ("TEXT", False),
    "NVARCHAR": ("VARCHAR", True),
    "NCHAR": ("CHAR", True),
    "BLOB": ("BYTEA", False),
    "LONGBLOB": ("BYTEA", False),
    "BINARY": ("BYTEA", False),
    "VARBINARY": ("BYTEA", False),
    "TEXT": ("TEXT", False),  # TEXT(n) is not valid in PostgreSQL
}


def _ident(name: str, what: str) -> str:
    """Return a safe identifier, quoting only when necessary."""
    if not isinstance(name, str) or not name.strip():
        raise DDLGenerationError(f"Empty {what} name.")
    if "\x00" in name:
        raise DDLGenerationError(f"Invalid character in {what} name {name!r}.")
    if len(name.encode("utf-8")) > _MAX_IDENT_LEN:
        raise DDLGenerationError(
            f"{what.capitalize()} name {name[:30]!r}... exceeds PostgreSQL's "
            f"{_MAX_IDENT_LEN}-byte identifier limit."
        )
    if _SIMPLE_IDENT.match(name) and name not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def _pg_type(data_type: str, column: str) -> str:
    """Validate a column type against a strict pattern and map it to PostgreSQL."""
    match = _TYPE_PATTERN.match((data_type or "").strip())
    if not match:
        raise DDLGenerationError(
            f"Column {column!r} has unsupported data type {data_type!r}; "
            "expected a type name with optional numeric precision, e.g. VARCHAR(100)."
        )
    base = " ".join(match.group(1).upper().split())
    params = re.sub(r"\s+", "", match.group(2) or "")
    if base in _TYPE_MAP:
        pg_base, keep_params = _TYPE_MAP[base]
        return pg_base + (params if keep_params else "")
    return base + params


def _creation_order(tables: List[TableDef]) -> List[TableDef]:
    """Order tables so referenced tables are created first (Kahn's algorithm,
    stable with respect to input order). Tables caught in a reference cycle are
    appended at the end; their cross-cycle foreign keys are deferred to ALTER
    TABLE statements by generate_ddl."""
    names = {t.name for t in tables}
    deps: Dict[str, Set[str]] = {}
    for t in tables:
        d: Set[str] = set()
        for c in t.columns:
            if c.foreign_key:
                ref_table = c.foreign_key.partition(".")[0]
                if ref_table in names and ref_table != t.name:
                    d.add(ref_table)
        deps[t.name] = d

    ordered: List[TableDef] = []
    placed: Set[str] = set()
    remaining = list(tables)
    while remaining:
        progressed = False
        for t in list(remaining):
            if deps[t.name] <= placed:
                ordered.append(t)
                placed.add(t.name)
                remaining.remove(t)
                progressed = True
        if not progressed:  # cycle: emit the rest in original order
            ordered.extend(remaining)
            break
    return ordered


def _pk_columns(table: TableDef) -> List[str]:
    return list(table.primary_key) or [c.name for c in table.columns if c.primary_key]


def generate_ddl(schema: CandidateSchema) -> str:
    """Generate PostgreSQL DDL for a schema. Raises DDLGenerationError if the
    schema cannot be expressed as safe, valid DDL."""
    if not schema.tables:
        raise DDLGenerationError("Schema has no tables.")

    by_name: Dict[str, TableDef] = {}
    for t in schema.tables:
        if t.name in by_name:
            raise DDLGenerationError(f"Duplicate table name {t.name!r}.")
        by_name[t.name] = t

    statements: List[str] = ["-- Generated by Smart Database Schema Designer (PostgreSQL)"]
    deferred: List[str] = []
    created: Set[str] = set()

    for table in _creation_order(schema.tables):
        t_ident = _ident(table.name, "table")
        if not table.columns:
            raise DDLGenerationError(f"Table {table.name!r} has no columns.")

        col_names = [c.name for c in table.columns]
        if len(set(col_names)) != len(col_names):
            raise DDLGenerationError(f"Table {table.name!r} has duplicate column names.")

        pk_cols = _pk_columns(table)
        for pk in pk_cols:
            if pk not in col_names:
                raise DDLGenerationError(
                    f"Primary key column {pk!r} is not a column of table {table.name!r}."
                )

        lines: List[str] = []
        comments: List[str] = []
        for col in table.columns:
            line = f"{_ident(col.name, 'column')} {_pg_type(col.data_type, col.name)}"
            if not col.nullable and col.name not in pk_cols:
                line += " NOT NULL"
            lines.append(line)

        if pk_cols:
            lines.append("PRIMARY KEY (" + ", ".join(_ident(c, "column") for c in pk_cols) + ")")

        table_created_after = created | {table.name}  # self-references are allowed inline
        for col in table.columns:
            if not col.foreign_key:
                continue
            ref_table, dot, ref_col = col.foreign_key.partition(".")
            target = by_name.get(ref_table)
            if not dot or target is None or ref_col not in [c.name for c in target.columns]:
                comments.append(
                    f"-- skipped foreign key {table.name}.{col.name} -> {col.foreign_key}: "
                    "referenced table/column does not exist in this schema"
                )
                continue
            if _pk_columns(target) != [ref_col]:
                comments.append(
                    f"-- WARNING: {table.name}.{col.name} references {ref_table}.{ref_col}, "
                    f"which is not {ref_table}'s sole primary key column; PostgreSQL rejects "
                    "this unless that column has a UNIQUE constraint"
                )
            constraint = _ident(f"fk_{table.name}_{col.name}"[:_MAX_IDENT_LEN], "constraint")
            fk_sql = (
                f"FOREIGN KEY ({_ident(col.name, 'column')}) "
                f"REFERENCES {_ident(ref_table, 'table')} ({_ident(ref_col, 'column')})"
            )
            if ref_table in table_created_after:
                lines.append(f"CONSTRAINT {constraint} {fk_sql}")
            else:  # referenced table not created yet (reference cycle)
                deferred.append(f"ALTER TABLE {t_ident} ADD CONSTRAINT {constraint} {fk_sql};")

        body = ",\n".join("    " + ln for ln in lines)
        prefix = "\n".join(comments) + "\n" if comments else ""
        statements.append(f"{prefix}CREATE TABLE {t_ident} (\n{body}\n);")
        created.add(table.name)

    statements.extend(deferred)
    return "\n\n".join(statements) + "\n"

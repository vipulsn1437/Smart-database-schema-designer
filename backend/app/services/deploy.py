"""
Sandboxed live deployment (PostgreSQL).

Takes DDL produced by app.services.ddl and actually runs it against a real
database, reporting exactly what was created, or exactly why PostgreSQL
refused it.

Isolation model
---------------
Every deployment gets its own freshly created schema (sandbox_<12 hex>),
and everything happens inside ONE transaction:

    BEGIN
      CREATE SCHEMA sandbox_xxxx
      SET LOCAL search_path TO sandbox_xxxx     -- DDL can only land here
      <the DDL>
      <introspect what was created>
    COMMIT   (keep=True)   or   ROLLBACK (keep=False, a "dry run")

PostgreSQL DDL is transactional, so a failed or dry-run deployment leaves
NOTHING behind, even if this process dies halfway. The user's own tables in
the `public` schema are never in the search path and are never touched.

Only DDL that this project generated is ever executed: the API accepts a
schema, not raw SQL, and ddl.py validates every identifier and type.
"""

from __future__ import annotations

import re
import secrets
from typing import Dict, List

import psycopg
from psycopg import sql

from app.models.schema_models import DeployedColumn, DeployedTable, DeployResponse

SANDBOX_NAME_PATTERN = re.compile(r"^sandbox_[0-9a-f]{12}$")


class DeploymentUnavailable(Exception):
    """The database could not be reached (wrong URL, server down, bad password)."""


class InvalidSandboxName(ValueError):
    """The given schema name is not one this feature creates."""


def _describe(error: psycopg.Error) -> str:
    """Turn a database error into one readable message, e.g.
    'there is no unique constraint matching given keys for referenced table "agents"'."""
    diag = getattr(error, "diag", None)
    primary = getattr(diag, "message_primary", None) or str(error).splitlines()[0]
    parts = [primary]
    for extra in (getattr(diag, "message_detail", None), getattr(diag, "message_hint", None)):
        if extra:
            parts.append(extra)
    return " | ".join(parts)


class SandboxDeployer:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._dsn, connect_timeout=5)
        except psycopg.Error as e:
            # Deliberately do not echo the connection string (it holds a password).
            raise DeploymentUnavailable(
                "Could not connect to the database. Check that PostgreSQL is running "
                f"and that DATABASE_URL is correct. ({_describe(e)})"
            ) from e

    def deploy(self, ddl: str, keep: bool = False) -> DeployResponse:
        """Run `ddl` in a fresh sandbox schema. keep=False rolls everything back
        after introspecting (a dry run); keep=True commits and returns the schema name."""
        schema_name = f"sandbox_{secrets.token_hex(6)}"
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
                cur.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema_name)))
                cur.execute("SET LOCAL statement_timeout = '15s'")
                cur.execute(ddl)  # no parameters -> multiple statements are allowed
                tables = self._introspect(cur, schema_name)
            if keep:
                conn.commit()
            else:
                conn.rollback()
            return DeployResponse(
                deployed=True,
                kept=keep,
                schema_name=schema_name if keep else None,
                tables=tables,
                ddl=ddl,
            )
        except psycopg.Error as e:
            conn.rollback()
            return DeployResponse(deployed=False, kept=False, schema_name=None, tables=[],
                                  ddl=ddl, error=_describe(e))
        finally:
            conn.close()

    def teardown(self, schema_name: str) -> bool:
        """Drop a kept sandbox schema. Returns False if it did not exist.
        Only names this feature generates are accepted, so this cannot be
        pointed at `public` or any other schema."""
        if not SANDBOX_NAME_PATTERN.match(schema_name):
            raise InvalidSandboxName(
                "Not a sandbox schema name (expected sandbox_ followed by 12 hex characters)."
            )
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema_name,))
                if cur.fetchone() is None:
                    return False
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))
            conn.commit()
            return True
        finally:
            conn.close()

    @staticmethod
    def _introspect(cur: psycopg.Cursor, schema_name: str) -> List[DeployedTable]:
        """Read back what PostgreSQL actually created (not what we asked for)."""
        cur.execute(
            """
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
            """,
            (schema_name,),
        )
        tables: Dict[str, DeployedTable] = {}
        for table, column, col_type, not_null in cur.fetchall():
            t = tables.setdefault(table, DeployedTable(name=table, columns=[], constraints=[]))
            t.columns.append(DeployedColumn(name=column, type=col_type, nullable=not not_null))

        cur.execute(
            """
            SELECT c.relname, pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND con.contype IN ('p', 'f')
            ORDER BY c.relname, con.contype DESC, con.conname
            """,
            (schema_name,),
        )
        for table, definition in cur.fetchall():
            if table in tables:
                tables[table].constraints.append(definition)
        return list(tables.values())

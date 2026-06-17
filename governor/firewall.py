"""SQL firewall: structural, read-only validation of LLM-generated queries.

Everything here is pure and synchronous — no database, no agent, no network.
It turns a candidate query into a syntax tree and refuses anything that isn't
a single, bounded, read-only SELECT.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from pydantic_ai import ModelRetry


def parse_single_statement(sql: str) -> exp.Expression:
    """Parse `sql` as PostgreSQL and return its syntax tree.

    Raises ModelRetry (so the agent can fix itself) if the text is empty,
    unparseable, or contains more than one statement.
    """
    text = sql.strip().rstrip(";")
    if not text:
        raise ModelRetry("The query was empty. Return a single read-only SELECT statement.")

    try:
        statements = [s for s in sqlglot.parse(text, dialect="postgres") if s is not None]
    except ParseError as exc:
        raise ModelRetry(
            f"That isn't valid PostgreSQL: {exc}. Return one syntactically correct SELECT."
        ) from exc

    if len(statements) != 1:
        raise ModelRetry(
            f"Detected {len(statements)} statements. Stacked/multi-statement queries are forbidden — "
            "send exactly one SELECT with no semicolons in the middle."
        )

    return statements[0]


# --- read-only enforcement -------------------------------------------------

# Operations that change data, schema, or permissions — never allowed.
_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,        # change data
    exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,   # change schema
    exp.Command,                                          # raw commands (GRANT, SET, ...)
)

# The only statement shapes allowed at the top of the tree.
_READ_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)


def ensure_read_only_select(tree: exp.Expression) -> exp.Expression:
    """Reject anything that isn't a pure read.

    Walks the entire tree, so a write hidden inside a CTE or subquery is
    caught too. Raises ModelRetry on any violation; returns the tree if clean.
    """
    # 1. The top-level operation must be a query, not a write/DDL/DCL.
    if not isinstance(tree, _READ_ROOTS):
        raise ModelRetry(
            f"The top-level operation is '{tree.key.upper()}', which is not a read. "
            "Only SELECT statements are allowed — rewrite it as a SELECT."
        )

    # 2. No write / Data Definiton Language (DDL) /Date Control Language (DCL) operation may appear ANYWHERE in the tree.
    # We all Data Query Language (DQL)
    offender = next(iter(tree.find_all(*_FORBIDDEN)), None)
    if offender is not None:
        raise ModelRetry(
            f"Found a forbidden '{offender.key.upper()}' operation inside the query. "
            "Remove any data- or schema-modifying statements; reads only."
        )

    # 3. `SELECT ... INTO new_table` quietly creates a table — also a write.
    for select in tree.find_all(exp.Select):
        if select.args.get("into") is not None:
            raise ModelRetry(
                "`SELECT ... INTO` creates a new table, which is a write. Remove the INTO clause."
            )

    return tree

# --- bounds & dangerous functions ------------------------------------------

DEFAULT_MAX_ROWS = 1000

# Server-side functions that read files, reach the network, or stall the DB.
_DANGEROUS_FUNCTIONS = (
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "pg_stat_file", "lo_import", "lo_export", "dblink",
    "current_setting", "set_config", "query_to_xml",
)


def require_bounded_limit(tree: exp.Expression, max_rows: int = DEFAULT_MAX_ROWS) -> exp.Expression:
    """Require an explicit integer LIMIT no larger than `max_rows`."""
    limit = tree.args.get("limit")
    if limit is None:
        raise ModelRetry(
            f"The query has no LIMIT. Add an explicit `LIMIT <= {max_rows}` so it can't scan the whole table."
        )

    value = limit.expression
    if not isinstance(value, exp.Literal) or value.is_string:
        raise ModelRetry(
            "LIMIT must be a plain integer like `LIMIT 100` — not a string, expression, or parameter."
        )

    try:
        rows = int(value.name)
    except ValueError:
        raise ModelRetry("LIMIT must be a whole number.")

    if rows <= 0:
        raise ModelRetry("LIMIT must be a positive integer.")
    if rows > max_rows:
        raise ModelRetry(f"LIMIT {rows} exceeds the cap of {max_rows}. Lower it to {max_rows} or fewer.")

    return tree


def block_dangerous_functions(tree: exp.Expression) -> exp.Expression:
    """Block server-side functions that touch files, the network, or stall the DB."""
    for fn in tree.find_all(exp.Anonymous):
        name = str(fn.this).lower()
        if name in _DANGEROUS_FUNCTIONS:
            raise ModelRetry(
                f"The function `{name}()` is blocked — it can reach the filesystem or network, "
                "or stall the database. Remove it and use plain SQL."
            )
    return tree

def validate_query(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> exp.Expression:
    """Run the full firewall over a candidate query.

    Returns the validated (unchanged) syntax tree, or raises ModelRetry on the
    first rule it breaks. This is the single entry point the agent will call.
    """
    tree = parse_single_statement(sql)
    tree = ensure_read_only_select(tree)
    tree = require_bounded_limit(tree, max_rows=max_rows)
    tree = block_dangerous_functions(tree)
    return tree
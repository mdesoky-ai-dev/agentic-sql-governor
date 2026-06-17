
"""Core orchestration: the PydanticAI agent that governs Text-to-SQL requests.

Flow:
  user question
    -> agent writes SQL
    -> validate_and_dry_run_query tool: firewall + EXPLAIN
    -> agent commits a SQLGovernorResponse
    -> output validator re-runs firewall (second, unconditional veto)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Union

from dotenv import load_dotenv
from pydantic_ai import Agent, ModelRetry, RunContext

from governor.database import DatabaseConn, DatabaseError
from governor.firewall import validate_query
from governor.models import (
    SecurityViolationResponse,
    SQLGovernorResponse,
    SuccessResponse,
)

load_dotenv()


# ---------------------------------------------------------------------------
# 1. DEPENDENCIES
# ---------------------------------------------------------------------------

@dataclass
class GovernorDeps:
    """Runtime dependencies injected into every tool call."""
    db: DatabaseConn
    read_only: bool = True


# ---------------------------------------------------------------------------
# 2. SCHEMA (fed verbatim into the system prompt)
# ---------------------------------------------------------------------------

DB_SCHEMA = """
-- users(user_id PK, email, full_name, country, kyc_status, created_at)
-- accounts(account_id PK, user_id FK->users, account_type, currency, status, opened_at)
-- balances(account_id PK FK->accounts, available_balance NUMERIC, ledger_balance NUMERIC, as_of)
-- transactions(txn_id PK, account_id FK->accounts, counterparty_account_id,
--              amount NUMERIC, currency, direction, status, txn_type, created_at)
""".strip()


# ---------------------------------------------------------------------------
# 3. AGENT
# ---------------------------------------------------------------------------

STATIC_INSTRUCTIONS = f"""
You are a secure Text-to-SQL agent for Desoky Capital LLC.

SCHEMA:
{DB_SCHEMA}

RULES (security has unconditional veto):
1. Only generate a single, read-only SELECT statement. Never INSERT, UPDATE,
   DELETE, DROP, CREATE, TRUNCATE, or any other write / DDL / DCL operation.
2. Every query MUST include an explicit LIMIT <= 1000.
3. Never use pg_sleep, pg_read_file, dblink, or any function that touches
   the filesystem, network, or database internals.
4. Resolve vague time expressions (e.g. "last quarter", "this year",
   "recent") to explicit ISO-8601 timestamps using the current UTC time
   supplied in your dynamic instructions. Never leave date logic ambiguous.
5. Always call the `validate_and_dry_run_query` tool before committing
   your final answer. If the tool raises an error, rewrite the query and
   call it again — do not return an answer without a successful tool call.
6. If the request cannot be answered with a safe SELECT (e.g. asks to modify
   data, or is ambiguous beyond recovery), return a SecurityViolationResponse
   explaining why and what the user could change.
""".strip()


governor_agent: Agent[GovernorDeps, SQLGovernorResponse] = Agent(
    model=os.getenv("LLM_MODEL", "openai-chat:gpt-4o-mini"),
    deps_type=GovernorDeps,
    output_type=Union[SuccessResponse, SecurityViolationResponse],  # type: ignore[arg-type]
    instructions=STATIC_INSTRUCTIONS,
)


# ---------------------------------------------------------------------------
# 4. DYNAMIC INSTRUCTIONS (current UTC time so dates resolve correctly)
# ---------------------------------------------------------------------------

@governor_agent.instructions
async def add_current_time(ctx: RunContext[GovernorDeps]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"Current UTC time: {now}. Use this to resolve all relative date expressions."


# ---------------------------------------------------------------------------
# 5. TOOL — the firewall gate
# ---------------------------------------------------------------------------

@governor_agent.tool
async def validate_and_dry_run_query(
    ctx: RunContext[GovernorDeps], sql_query: str
) -> str:
    """Validate and dry-run a candidate SQL query.

    Runs the four-gate AST firewall, then asks Postgres to EXPLAIN the query.
    Returns the query plan on success so the agent can confirm it is correct.
    Raises ModelRetry with specific feedback so the agent can self-correct.
    """
    # Gates 1-4: AST firewall (parse, read-only, limit, dangerous functions).
    try:
        validate_query(sql_query)
    except ModelRetry:
        raise  # firewall message is already well-formed for the LLM

    # Gate 5: database dry-run — catches unknown tables / columns / syntax.
    try:
        plan = await ctx.deps.db.dry_run(sql_query)
    except DatabaseError as exc:
        raise ModelRetry(
            f"Postgres rejected the query during EXPLAIN: {exc}. "
            "Fix the table or column names and resubmit."
        ) from exc

    return f"Query is valid. Postgres plan:\n{plan}"


# ---------------------------------------------------------------------------
# 6. OUTPUT VALIDATOR — second, unconditional firewall veto
# ---------------------------------------------------------------------------

@governor_agent.output_validator
async def re_validate_final_query(
    ctx: RunContext[GovernorDeps], output: SQLGovernorResponse
) -> SQLGovernorResponse:
    """Re-run the firewall on the committed query — the second independent veto.

    The model cannot skip the tool and fabricate a SuccessResponse.
    If the final SQL fails the firewall, the agent must retry.
    """
    if isinstance(output, SuccessResponse):
        try:
            validate_query(output.sql_query)
        except ModelRetry as exc:
            raise ModelRetry(
                f"Final query failed the security re-check: {exc}. Rewrite it."
            ) from exc
    return output


# ---------------------------------------------------------------------------
# 7. PUBLIC ENTRY POINT
# ---------------------------------------------------------------------------

async def ask(question: str, db: DatabaseConn) -> SQLGovernorResponse:
    """Ask a plain-English question; get back a governed SQL response."""
    deps = GovernorDeps(db=db, read_only=True)
    result = await governor_agent.run(question, deps=deps)
    return result.output
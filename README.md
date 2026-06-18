# Agentic SQL Governor

**Security-first Text-to-SQL engine for Desoky Capital LLC.**  
Ask a financial database questions in plain English — while a multi-layer security governor ensures the AI can only ever *look*, never *touch*.

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![PydanticAI](https://img.shields.io/badge/PydanticAI-1.x-purple)](https://ai.pydantic.dev)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-teal)](https://neon.tech)
[![Tests](https://img.shields.io/badge/Tests-20%20passing-brightgreen)]()

---

## What It Does

A user asks: *"Which customers moved the most money last quarter?"*

The engine:
1. Translates the plain-English question into a PostgreSQL SELECT
2. Runs it through a **4-gate AST firewall** (parse → read-only → bounded LIMIT → no dangerous functions)
3. Validates it against the live database via `EXPLAIN` without touching data
4. Returns a structured JSON response with the SQL, a plain-English explanation, and a chart configuration

If the request cannot be answered safely — e.g. "delete all users" — the engine returns a structured `SecurityViolationResponse` with the reason and remediation steps. The database is never touched.

---

## Security Architecture

The project's core claim: **the AI cannot run an unsafe query, even if it tries.**

Two independent security layers enforce this:

### Layer 1 — SQLGlot AST Firewall (`governor/firewall.py`)

Uses SQLGlot to parse every LLM-generated query into a syntax tree and reject:

| Attack Class | Example | How It's Caught |
|---|---|---|
| Multi-statement injection | `SELECT 1; DROP TABLE users` | `len(statements) != 1` |
| Direct write/DDL/DCL | `DELETE FROM users` | Root node not in `_READ_ROOTS` |
| Hidden write in CTE | `WITH t AS (DELETE FROM users RETURNING *) SELECT * FROM t` | `tree.find_all(*_FORBIDDEN)` |
| `SELECT … INTO` | `SELECT * INTO copy_tbl FROM users` | `select.args.get("into")` check |
| Unbounded scan | `SELECT * FROM transactions` (no LIMIT) | `limit.args.get("limit") is None` |
| Oversized result | `SELECT * FROM users LIMIT 999999` | `rows > DEFAULT_MAX_ROWS (1000)` |
| Filesystem / network functions | `SELECT pg_read_file('/etc/passwd')` | `exp.Anonymous` name in `_DANGEROUS_FUNCTIONS` |
| Database stall | `SELECT pg_sleep(30)` | Same as above |

### Layer 2 — Read-Only Database Role

The application connects to Neon Postgres as `sql_governor_ro` — a role with `SELECT` only, physically incapable of writing. Even if a query passed the firewall, the database itself would refuse it.

Both layers are **independent**. Defeating one doesn't defeat the other.

---

## Agent Architecture

```
User Question (plain English)
      │
      ▼
PydanticAI Agent  ←─ STATIC_INSTRUCTIONS (schema + security rules)
      │            ←─ @instructions (current UTC time for date normalization)
      │
      ▼
Claude writes SQL
      │
      ▼
@agent.tool: validate_and_dry_run_query
      ├── Gate 1: SQLGlot parse (valid SQL? single statement?)
      ├── Gate 2: Read-only check (SELECT only, no hidden writes)
      ├── Gate 3: LIMIT enforcement (required, capped at 1000)
      ├── Gate 4: Dangerous function blocklist
      └── Gate 5: Postgres EXPLAIN dry-run (catches unknown tables/columns)
            │
            ├── FAIL → ModelRetry (LLM rewrites and tries again)
            └── PASS → Claude commits final answer
                          │
                          ▼
                @agent.output_validator (second, unconditional firewall veto)
                          │
                          ▼
              SQLGovernorResponse
              ├── SuccessResponse    { sql_query, explanation, visual_schema }
              └── SecurityViolationResponse  { violation_reason, remediation_steps }
```

---

## Stack

| Layer | Technology |
|---|---|
| Agent framework | PydanticAI 1.x |
| LLM | Anthropic Claude Haiku 4.5 |
| SQL validation | SQLGlot (AST parsing, Postgres dialect) |
| Database | PostgreSQL on Neon (read-only role) |
| Async DB driver | asyncpg |
| API | FastAPI + uvicorn |
| Output validation | Pydantic v2 |
| Tests | pytest (20 attack scenarios) |

---

## Project Structure

```
agentic-sql-governor/
├── governor/
│   ├── firewall.py      # 4-gate AST security firewall
│   ├── models.py        # Pydantic output models (SuccessResponse / SecurityViolationResponse)
│   ├── database.py      # Async Neon connection pool (read-only role)
│   ├── engine.py        # PydanticAI agent, tool, output validator
│   └── schema.py        # Canonical DB schema
├── app/
│   └── main.py          # FastAPI: POST /ask, GET /health
├── tests/
│   └── test_firewall.py # 20 attack tests — 4 safe queries pass, 16 attacks blocked
├── .env.example
└── requirements.txt
```

---

## Live Demo

**API:** `https://agentic-sql-governor.onrender.com`  
**Docs:** `https://agentic-sql-governor.onrender.com/docs`

Try it:
```bash
curl -X POST https://agentic-sql-governor.onrender.com/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which users have the highest available balance?"}'
```

---

## Local Setup

**Prerequisites:** Python 3.11+, Anaconda or venv, a Neon Postgres account, an Anthropic API key.

```bash
# 1. Clone and enter
git clone https://github.com/mdesoky-ai-dev/agentic-sql-governor.git
cd agentic-sql-governor

# 2. Create environment
conda create -n sqlgov python=3.11 -y
conda activate sqlgov

# 3. Install dependencies
python -m pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — add DATABASE_URL, ANTHROPIC_API_KEY, LLM_MODEL

# 5. Run the firewall test suite
python -m pytest -q

# 6. Start the server
uvicorn app.main:app --reload
# Open http://127.0.0.1:8000/docs
```

---

## Environment Variables

```bash
DATABASE_URL=postgresql://sql_governor_ro:password@host.neon.tech/neondb?sslmode=require
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=anthropic:claude-haiku-4-5-20251001
```

The `DATABASE_URL` should use your **read-only role** credentials, not the database owner. See the database setup section below.

---

## Database Setup

Run this in your Neon SQL Editor to create the fintech schema and seed data, then create the read-only role:

```sql
-- Create read-only role
CREATE ROLE sql_governor_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'your_password';
GRANT CONNECT ON DATABASE neondb TO sql_governor_ro;
GRANT pg_read_all_data TO sql_governor_ro;
```

The full schema (users, accounts, balances, transactions) is in `governor/schema.py`.

---

## Test Suite

```bash
python -m pytest -q
```

```
20 passed in 0.11s
```

The test suite (`tests/test_firewall.py`) fires 16 attack scenarios at the firewall and asserts every one raises `ModelRetry`. Four legitimate queries are also tested to confirm they pass. This makes the "governance" claim verifiable, not just asserted.

Attack classes covered: stacked statements, DROP/DELETE/UPDATE/INSERT/TRUNCATE, hidden CTE deletes, SELECT INTO, missing LIMIT, oversized LIMIT, non-integer LIMIT, pg_sleep DoS, pg_read_file filesystem read, dblink network exfiltration, garbage input, empty input.

---

## How It Differs from the Other Projects

| Project | Core Challenge | Framework |
|---|---|---|
| `fin-underwriter-agent` | Multi-agent credit scoring + Basel III compliance | LangGraph + AWS Bedrock |
| `graphrag-financial-intelligence` | Hybrid knowledge graph retrieval + evaluation | Neo4j + Pinecone + Ragas |
| `agentic-sql-governor` | SQL security governance + AST-level attack prevention | PydanticAI + SQLGlot |

This project is the only one in the portfolio that operates at the SQL/AST level. The governance pattern — LLM generates, firewall vetoes, database physically enforces — is applicable to any natural-language-to-query interface over sensitive data.

---

## Author

**Mohamed Desoky, PhD, MBA**  
Academic Director, Master of FinTech & Analytics — Wake Forest University  
[github.com/mdesoky-ai-dev](https://github.com/mdesoky-ai-dev)
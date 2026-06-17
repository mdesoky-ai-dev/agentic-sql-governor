"""FastAPI entry point for the SQL Governor.

Single endpoint: POST /ask
  body:    { "question": "which users have the highest balance?" }
  returns: SQLGovernorResponse (SuccessResponse or SecurityViolationResponse)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from governor.database import NeonDatabase
from governor.engine import ask
from governor.models import SQLGovernorResponse


# ---------------------------------------------------------------------------
# 1. DATABASE LIFECYCLE  (one pool for the life of the app)
# ---------------------------------------------------------------------------

_db: NeonDatabase | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Open the DB pool on startup, close it on shutdown."""
    global _db
    _db = await NeonDatabase.connect()
    yield
    await _db.close()


# ---------------------------------------------------------------------------
# 2. APP
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Agentic SQL Governor",
    description="Security-first Text-to-SQL engine for Desoky Capital LLC.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 3. REQUEST / RESPONSE SCHEMAS
# ---------------------------------------------------------------------------

class QuestionRequest(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# 4. ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — used by Render to confirm the app is up."""
    return {"status": "ok"}


@app.post("/ask", response_model=None)
async def ask_endpoint(body: QuestionRequest) -> SQLGovernorResponse:
    """Translate a plain-English question into a governed SQL response."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not ready.")
    return await ask(body.question, _db)
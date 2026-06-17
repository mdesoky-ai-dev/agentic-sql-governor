"""Structured outputs for the SQL governor agent.

The agent must finish in exactly one of two states:
  * SuccessResponse           — a safe, validated query and how to present it.
  * SecurityViolationResponse — a refusal, with the reason and how to fix it.
"""
from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, Field


class ChartConfig(BaseModel):
    """How a client should visualize the result set."""

    chart_type: Literal["bar", "line", "pie", "area", "scatter", "table"]
    x_axis: str | None = Field(default=None, description="Column for the x-axis / category.")
    y_axis: str | None = Field(default=None, description="Column for the y-axis / value.")
    series: list[str] = Field(default_factory=list, description="Extra columns to plot as separate series.")
    title: str = Field(description="Human-readable chart title.")


class SuccessResponse(BaseModel):
    """A validated, safe-to-run query and how to present it."""

    sql_query: str = Field(description="The validated, read-only PostgreSQL SELECT.")
    explanation: str = Field(description="Markdown explaining what the query does and its assumptions.")
    visual_schema: ChartConfig = Field(description="Suggested chart configuration for the result set.")


class SecurityViolationResponse(BaseModel):
    """A refusal: the request could not be answered safely."""

    violation_reason: str = Field(description="Why the request was refused.")
    remediation_steps: str = Field(description="What the user could change to make it answerable.")


SQLGovernorResponse: TypeAlias = SuccessResponse | SecurityViolationResponse
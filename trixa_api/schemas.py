"""Pydantic-schemas för request/response-validering."""

from __future__ import annotations

from datetime import date as date_type
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- Plan-generation ----------


class GeneratePlanRequest(BaseModel):
    athlete_user_id: str = Field(..., description="auth.users.id för adepten")
    week_start: date_type = Field(..., description="Måndag-datum för veckan")
    week_in_period: int = Field(1, ge=1, le=12)
    weeks_in_period: int = Field(6, ge=1, le=12)
    apply: bool = Field(False, description="Skriv till DB. Default är dry-run.")


class WorkoutSummary(BaseModel):
    date: date_type
    sport: str
    code: str
    title: str
    category: str
    duration_minutes: int
    intensity: str
    notes: str = ""


class WeekPlanResponse(BaseModel):
    athlete_id: str
    athlete_user_id: str
    week_start: date_type
    phase: str
    period: str | None
    total_hours_target: float
    discipline_hours: dict[str, float]
    categories: list[str]
    strength_protocol: str
    overtraining_level: str
    overtraining_flags: list[str]
    plan_adjustment: dict | None
    workouts: list[WorkoutSummary]
    warnings: list[str]
    persisted_week_id: str | None = None
    alerts_written: int = 0


# ---------- Override ----------


class CoachOverrideRequest(BaseModel):
    athlete_user_id: str
    scope: Literal["week", "workout", "phase", "volume", "overtraining"]
    week_id: str | None = None
    workout_id: str | None = None
    engine_recommendation: dict
    override_decision: dict
    motivation: str = Field(..., min_length=10)
    medical_context_disclosed: bool = False
    athlete_explicit_request: bool = False


class CoachOverrideResponse(BaseModel):
    id: str
    athlete_id: str
    scope: str
    motivation: str
    is_active: bool
    created_at: str


# ---------- Weekly report ----------


class PainLocation(BaseModel):
    location: str
    severity: int = Field(..., ge=1, le=5)
    affects_disciplines: list[str] = []
    since: date_type | None = None


class WeeklyReportRequest(BaseModel):
    athlete_user_id: str
    week_start: date_type
    sleep_quality: int | None = Field(None, ge=1, le=5)
    motivation: int | None = Field(None, ge=1, le=5)
    soreness: int | None = Field(None, ge=1, le=5)
    energy: int | None = Field(None, ge=1, le=5)
    stress: int | None = Field(None, ge=1, le=5)
    pain_present: bool = False
    injury_change: bool = False
    illness_present: bool = False
    travel_planned: bool = False
    pain_locations: list[PainLocation] = []
    notes: str = ""


# ---------- Athlete ----------


class AthleteResponse(BaseModel):
    id: str
    user_id: str
    goal: str
    sports: list[str]
    experience_level: str
    weekly_hours: float
    race_type: str | None
    race_date: date_type | None
    time_goal: str | None
    ftp: int | None
    lthr: int | None
    swim_css: str | None
    run_threshold_pace: str | None
    health_conditions: list[dict]
    active_concerns: list[dict]
    medications: list[dict]
    phase_state: dict
    notes: str


# ---------- Alerts ----------


class AlertResponse(BaseModel):
    id: str
    alert_type: str
    severity: str
    title: str
    body: str
    data: dict
    is_read: bool
    is_dismissed: bool
    created_at: str

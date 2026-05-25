"""Strukturerade alerts — mappa engine-signaler till coach_alerts-rader.

Trixa kan inte tolka eller skriva fritext. Varje varning, eskalering eller
observation måste mappas till en deterministisk alert från `data/alerts.yaml`.
Coach (Nils) läser alerts i tråden och kan välja att kommentera dem mänskligt
— men beslutet att ALERTA är Trixas, inte Nils.

Användning:
    from coach.trixa.alerts import build_alerts, persist_alerts

    alerts = build_alerts(plan, athlete)
    persist_alerts(client, alerts, coach_user_id)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from coach.engine._loader import load_yaml

if TYPE_CHECKING:
    from coach.trixa.planner import WeekPlan


# ---------- Datatyp ----------


@dataclass
class Alert:
    """En konkret alert redo att skriva till coach_alerts-tabellen."""

    alert_type: str
    severity: str  # info | warning | critical
    title: str
    body: str
    data: dict = field(default_factory=dict)

    def to_db_row(self, athlete_id: str, athlete_user_id: str, coach_user_id: str) -> dict:
        return {
            "athlete_id": athlete_user_id,  # coach_alerts.athlete_id → auth.users.id
            "coach_id": coach_user_id,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "title": self.title,
            "body": self.body.strip(),
            "data": {
                **self.data,
                "athlete_profile_id": athlete_id,
            },
            "is_read": False,
            "is_dismissed": False,
        }


# ---------- Katalog-uppslag ----------


def _load_alerts_catalog() -> dict[str, dict]:
    """Läs alerts.yaml och returnera dict {alert_type: definition}."""
    data = load_yaml("alerts.yaml")
    return {a["alert_type"]: a for a in data.get("alerts", [])}


def _make_alert(catalog: dict, alert_type: str, data: dict | None = None) -> Alert | None:
    """Bygg en Alert från katalog-mall + valfri extra data."""
    template = catalog.get(alert_type)
    if not template:
        return None
    return Alert(
        alert_type=alert_type,
        severity=template["severity"],
        title=template["title"],
        body=template["body"],
        data=data or {},
    )


# ---------- Alert-byggare ----------


def build_alerts(plan: "WeekPlan", athlete: dict, today: date | None = None) -> list[Alert]:
    """Generera alla strukturerade alerts för en given vecka.

    Args:
        plan: färdig WeekPlan från planner.generate_week
        athlete: athlete_profiles-rad
        today: referensdatum (default: idag)

    Returns:
        Lista av Alert-objekt. Kan vara tom om allt är OK.
    """
    today = today or date.today()
    catalog = _load_alerts_catalog()
    alerts: list[Alert] = []

    # 1. Överträningsnivå
    ot_level = plan.overtraining_level
    if ot_level and ot_level != "none":
        a = _make_alert(
            catalog,
            f"overtraining_{ot_level}",
            data={"level": ot_level, "flags": list(plan.overtraining_flags)},
        )
        if a:
            alerts.append(a)

    # 2. Aktiva concerns som kräver follow-up
    for concern in athlete.get("active_concerns") or []:
        if concern.get("needs_followup"):
            a = _make_alert(
                catalog,
                "injury_needs_followup",
                data={
                    "concern_name": concern.get("name"),
                    "follow_up_by": concern.get("follow_up_by"),
                    "severity": concern.get("severity"),
                },
            )
            if a:
                alerts.append(a)

        # 3. Concerns som har varit aktiva > 14 dagar med severity >= 3
        since_str = concern.get("since_date")
        sev = concern.get("severity") or 0
        if since_str and sev >= 3:
            try:
                since = date.fromisoformat(str(since_str)[:10])
                days = (today - since).days
                if days > 14:
                    a = _make_alert(
                        catalog,
                        "injury_persists",
                        data={
                            "concern_name": concern.get("name"),
                            "since_date": str(since_str),
                            "days_active": days,
                            "severity": sev,
                        },
                    )
                    if a:
                        alerts.append(a)
            except (ValueError, TypeError):
                pass

    # 4. Plan-genererad (informativ)
    a = _make_alert(
        catalog,
        "plan_generated",
        data={
            "week_start": plan.week_start.isoformat(),
            "phase": plan.phase,
            "period": plan.period,
            "workout_count": len([w for w in plan.workouts if w.sport != "rest"]),
        },
    )
    if a:
        alerts.append(a)

    return alerts


# ---------- Persist ----------


def _find_coach_for_athlete(client, athlete_user_id: str) -> str | None:
    """Hitta aktiv coach för en adept via coach_athletes-tabellen."""
    # coach_athletes.status använder 'accepted' (efter invite-accept).
    # 'active' förekommer inte i nuvarande data men accepteras för framtid.
    res = (
        client.table("coach_athletes")
        .select("coach_id")
        .eq("athlete_id", athlete_user_id)
        .in_("status", ["accepted", "active"])
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]["coach_id"]
    return None


def persist_alerts(
    client,
    alerts: list[Alert],
    athlete_id: str,
    athlete_user_id: str,
    coach_user_id: str | None = None,
) -> list[dict]:
    """Skriv alerts till coach_alerts-tabellen.

    Args:
        client: Postgrest-klient (service-role)
        alerts: lista av Alert-objekt
        athlete_id: athlete_profiles.id (sparas i data jsonb för spårbarhet)
        athlete_user_id: auth.users.id (coach_alerts.athlete_id)
        coach_user_id: auth.users.id för coachen. Om None — slå upp via
            coach_athletes. Om ingen hittas: returnera tom lista med varning.

    Returns:
        Lista med insatta DB-rader.
    """
    if not alerts:
        return []

    if coach_user_id is None:
        coach_user_id = _find_coach_for_athlete(client, athlete_user_id)
    if coach_user_id is None:
        # Ingen coach kopplad — hoppa över alerts (de hör hemma i coach-inkorgen)
        return []

    rows = [a.to_db_row(athlete_id, athlete_user_id, coach_user_id) for a in alerts]
    res = client.table("coach_alerts").insert(rows).execute()
    return res.data or []

"""Trixa-API — FastAPI-skal som exponerar engine + planner + alerts.

Designprincip: tunt skal. Logik ligger i coach.trixa.*, API:t orkestrerar
HTTP + auth + serialisering.

Endpoints:
    GET  /health
    GET  /api/week/current?athlete_user_id=...
    GET  /api/athlete/{user_id}
    POST /api/plan/generate
    POST /api/override
    POST /api/weekly_report
    GET  /api/alerts?athlete_user_id=...
    GET  /api/workouts/{code}
"""

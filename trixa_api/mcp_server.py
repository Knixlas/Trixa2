"""MCP-server (``/mcp``) — Trixa som verktygslåda i en AI-klient.

Streamable HTTP + JSON-RPC 2.0. Auth är **samma per-adept-Bearer-token som
``/agent/*``** (se ``agent_auth``): token = identitet, varje verktyg låses till
EN adept, ingen ``athlete_user_id``-parameter finns att manipulera.

Varför en egen JSON-RPC-loop och inte MCP-SDK:t: ytan vi behöver är liten
(``initialize``, ``tools/list``, ``tools/call``, ``ping``) och Trixa håller
``requirements.txt`` kort med flit — samma skäl som fick postgrest att vinna
över supabase-metapaketet. Här kan hela transporten testas med FastAPI:s
TestClient utan att jaga ett SSE-lager vi ändå inte använder.

Servern är **stateless**: ingen session-id-hantering, inget serveröppnat
SSE-flöde. Varje POST bär sin egen token och besvaras med ``application/json``,
vilket streamable-HTTP-transporten uttryckligen tillåter.

Verktygen är tunna omslag runt funktionerna i ``agent_api`` — de anropas som
vanliga Python-funktioner, inte över HTTP, så det finns exakt en implementation
av varje regel (upsert-logik, sport-normalisering, override-nycklar).

Klient-setup: ``docs/11_MCP_CONNECTOR.md``.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_type
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from trixa_api import agent_api
from trixa_api.agent_auth import AgentScope, resolve_agent_scope, unauthorized_headers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

SERVER_NAME = "trixa"
SERVER_VERSION = "0.1.0"

# Protokollversioner vi kan tala. Klientens önskemål ekas tillbaka om det finns
# i listan, annars svarar vi med vår senaste (spec:ens förhandlingsregel).
_SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
_LATEST_PROTOCOL = _SUPPORTED_PROTOCOLS[0]

# JSON-RPC-felkoder
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


def _parse_date(raw: str, field: str) -> date_type:
    try:
        return date_type.fromisoformat(str(raw).strip())
    except (ValueError, TypeError):
        raise ValueError(
            f"{field} måste vara ett datum på formen YYYY-MM-DD (fick {raw!r})."
        )


# ---------- verktygen ----------


def _tool_whoami(scope: AgentScope, args: dict) -> Any:
    return agent_api.whoami(scope=scope)


def _tool_get_athlete(scope: AgentScope, args: dict) -> Any:
    return agent_api.get_athlete(scope=scope)


def _tool_get_constraints(scope: AgentScope, args: dict) -> Any:
    return agent_api.get_constraints(scope=scope)


def _tool_get_week(scope: AgentScope, args: dict) -> Any:
    monday = (args.get("monday") or "").strip()
    if not monday:
        return agent_api.get_current_week(scope=scope)
    return agent_api.get_week(monday=_parse_date(monday, "monday"), scope=scope)


def _tool_get_training_log(scope: AgentScope, args: dict) -> Any:
    since = (args.get("since") or "").strip()
    limit = int(args.get("limit") or 60)
    return agent_api.get_log(
        since=_parse_date(since, "since") if since else None,
        limit=max(1, min(limit, 365)),
        scope=scope,
    )


def _tool_get_recovery(scope: AgentScope, args: dict) -> Any:
    days = int(args.get("days") or 14)
    return agent_api.get_recovery(days=max(1, min(days, 90)), scope=scope)


def _tool_get_engine_week(scope: AgentScope, args: dict) -> Any:
    monday = (args.get("monday") or "").strip()
    return agent_api.get_engine_week(
        monday=_parse_date(monday, "monday") if monday else None, scope=scope,
    )


def _tool_list_workouts(scope: AgentScope, args: dict) -> Any:
    return agent_api.list_passbank(
        sport=(args.get("sport") or "").strip() or None,
        category=(args.get("category") or "").strip() or None,
        phase=(args.get("phase") or "").strip() or None,
        scope=scope,
    )


def _tool_get_session_feedback(scope: AgentScope, args: dict) -> Any:
    return agent_api.get_session_feedback(days=int(args.get("days") or 90), scope=scope)


def _tool_propose_workout(scope: AgentScope, args: dict) -> Any:
    return agent_api.propose_workout(body=agent_api.ProposalIn(**args), scope=scope)


def _tool_list_proposals(scope: AgentScope, args: dict) -> Any:
    return agent_api.list_proposals(scope=scope)


def _tool_plan_session(scope: AgentScope, args: dict) -> Any:
    body = agent_api.PlanSessionIn(
        date=_parse_date(args.get("date", ""), "date"),
        sport=args.get("sport", ""),
        title=args.get("title", ""),
        duration_min=args.get("duration_min"),
        intensity=args.get("intensity") or "",
        details=args.get("details") or "",
        workout_code=args.get("workout_code") or "",
        exercises=args.get("exercises") or [],
    )
    return agent_api.write_plan_session(body=body, scope=scope)


def _tool_delete_planned_session(scope: AgentScope, args: dict) -> Any:
    session_id = (args.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id krävs.")
    return agent_api.delete_plan_session(session_id=session_id, scope=scope)


def _tool_log_override(scope: AgentScope, args: dict) -> Any:
    week_start = (args.get("week_start") or "").strip()
    body = agent_api.OverrideIn(
        scope=args.get("scope", ""),
        engine_recommendation=args.get("engine_recommendation") or {},
        override_decision=args.get("override_decision") or {},
        motivation=args.get("motivation", ""),
        medical_context_disclosed=bool(args.get("medical_context_disclosed")),
        athlete_explicit_request=bool(args.get("athlete_explicit_request")),
        week_start=_parse_date(week_start, "week_start") if week_start else None,
        planned_session_id=(args.get("planned_session_id") or "").strip() or None,
    )
    return agent_api.write_override(body=body, scope=scope)


_SPORT_ENUM = ["bike", "run", "swim", "strength", "brick", "yoga", "rest"]

# (namn, beskrivning, inputSchema, handler). Beskrivningarna är det AI-klienten
# faktiskt ser — de ska räcka för att välja rätt verktyg utan att läsa docs.
_TOOLS: list[tuple[str, str, dict, Callable[[AgentScope, dict], Any]]] = [
    (
        "whoami",
        "Verifiera kopplingen: vilken adept är den här token:en låst till? "
        "Kör den först om du är osäker på att åtkomsten fungerar.",
        {"type": "object", "properties": {}},
        _tool_whoami,
    ),
    (
        "get_athlete",
        "Hela adeptprofilen: mål, erfarenhetsnivå, tröskelvärden, veckoram, "
        "aktiva discipliner, vilodagar, långpassdagar, utrustning och "
        "pool-tillgång, inne/ute-preferens, besvär med impact per gren, "
        "kroniska tillstånd, nutrition och fasläge. Samma fält som adepten ser "
        "på inställnings- och hälsosidan.",
        {"type": "object", "properties": {}},
        _tool_get_athlete,
    ),
    (
        "get_constraints",
        "Vad som ÖVERHUVUDTAGET går att planera, färdigsammanvägt: aktiva "
        "discipliner, grenar som är blockerade eller begränsade av besvär, "
        "vilodagar som ska lämnas tomma, pool-tillgång och utrustning. Läs den "
        "FÖRE du skriver pass — den hindrar dig från att lägga simpass åt någon "
        "utan pool eller träning på en låst vilodag.",
        {"type": "object", "properties": {}},
        _tool_get_constraints,
    ),
    (
        "get_week",
        "Planerade pass för en vecka. Utan argument: veckan som innehåller "
        "dagens datum. Fältet 'origin' visar vem som skapade passet — 'trixa2' "
        "är motorns, 'manual' adeptens eget, och dina egna får 'nils'. "
        "Styrkepassens 'exercises' bär nästa vikt och reps räknade ur förra "
        "passets loggade ansträngning ('suggestion': reason, trend, previous) "
        "— samma tal adeptens loggformulär visar. Föreslå inga andra vikter "
        "utan att säga varför du avviker.",
        {
            "type": "object",
            "properties": {
                "monday": {
                    "type": "string",
                    "description": "Veckans måndag, YYYY-MM-DD. Utelämna för innevarande vecka.",
                }
            },
        },
        _tool_get_week,
    ),
    (
        "get_training_log",
        "Genomförda pass ur adeptens träningslogg (alla källor: TrainingPeaks, "
        "Strava, manuellt). Använd för att jämföra plan mot utfall.",
        {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Från och med datum, YYYY-MM-DD. Default 28 dagar bakåt.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max antal pass (1-365, default 60).",
                },
            },
        },
        _tool_get_training_log,
    ),
    (
        "get_recovery",
        "Dygnsdata för återhämtning: vilopuls, HRV mot baseline, sömnpoäng, "
        "readiness, stress och belastningskvot (ACWR). Läs den innan du höjer "
        "belastningen eller lägger in kvalitetspass. Saknar adepten kopplad "
        "klocka svarar den has_data=false med en note — det är normalt, inte "
        "ett fel; planera då på veckoram och upplevd ansträngning.",
        {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Antal dygn bakåt (1-90, default 14).",
                }
            },
        },
        _tool_get_recovery,
    ),
    (
        "get_engine_week",
        "Motorns förslag för en vecka enligt Trixas träningslära, torrkört (sparas "
        "inte): fas, tillåtna passtyper, adeptens nivågränser (längsta pass per "
        "gren, antal pass, veckobudget i minuter), nedtrappning, loppdag och "
        "förslagen pass med workout_code. Läs den INNAN du planerar en vecka — "
        "den är utgångsläget. Dagar som redan har coachens pass står i "
        "coached_dates.",
        {
            "type": "object",
            "properties": {
                "monday": {
                    "type": "string",
                    "description": "Veckans måndag, YYYY-MM-DD. Utelämna för innevarande vecka.",
                }
            },
        },
        _tool_get_engine_week,
    ),
    (
        "list_workouts",
        "Passbanken: Trixas färdiga pass, filtrerade på adeptens nivå. Varje pass "
        "har workout_code, gren, passtyp, längdspann, faser och om passtypen "
        "är tillåten för nivån. Välj härifrån i stället för att skriva egna pass.",
        {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "enum": ["swim", "bike", "run", "strength", "brick"]},
                "category": {
                    "type": "string",
                    "description": (
                        "Passtyp: AE lugn uthållighet, SS snabbhet, TE tempo, ME tröskel, "
                        "MF kraft, AC intervaller, T test, BW kombinationspass, ST styrka."
                    ),
                },
                "phase": {
                    "type": "string",
                    "description": "Fas: prep, base, build, peak, race, recovery.",
                },
            },
        },
        _tool_list_workouts,
    ),
    (
        "plan_session",
        "Skriv ett pass i adeptens plan. Upsert på (datum, gren): samma dag och "
        "gren skrivs över så du kan korrigera utan att skapa dubbletter. Pass du "
        "skriver är skyddade — motorn genererar aldrig över dem. "
        "Skicka workout_code ur list_workouts eller get_engine_week: då hämtas "
        "passets text (på adeptens språk, med adeptens zoner), steg och "
        "övningar ur passbanken, och duration_min väljs inom passets spann. "
        "Din 'details' läggs då till som en kommentar från coachen. "
        "Okänd kod avvisas. Passet prövas mot adeptens nivå: för långt, fel "
        "passtyp eller över veckobudgeten ger 'warnings' — avviker du medvetet, "
        "dokumentera med log_override. "
        "STYRKEPASS utan workout_code: skicka alltid 'exercises'.",
        {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Passets datum, YYYY-MM-DD."},
                "sport": {"type": "string", "enum": _SPORT_ENUM},
                "title": {
                    "type": "string",
                    "description": "Kort passnamn, t.ex. 'Tröskel 3x10 min'.",
                },
                "duration_min": {
                    "type": "integer",
                    "description": "Total passtid i minuter.",
                },
                "intensity": {
                    "type": "string",
                    "description": "Zon eller intensitetsetikett, t.ex. 'Z2'.",
                },
                "details": {
                    "type": "string",
                    "description": "Passets upplägg i text: uppvärmning, huvuddel, nedvarvning.",
                },
                "workout_code": {
                    "type": "string",
                    "description": (
                        "Passbankskod ur list_workouts/get_engine_week, t.ex. "
                        "'AE1_run_gajogg'. Ger passets text, steg och övningar."
                    ),
                },
                "exercises": {
                    "type": "array",
                    "description": (
                        "Styrkepassets övningar som strukturerad lista. Adeptens "
                        "loggformulär förifylls från den — utan den får hen skriva "
                        "in varje övningsnamn för hand. Skriv den ALLTID för "
                        "styrkepass, utöver upplägget i details. Listan är också "
                        "vad lastprogressionen räknar på: reps_min/reps_max ger "
                        "spannet där reps växlas mot tyngre vikt."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Övningens namn, t.ex. 'Knäböj'."},
                            "code": {
                                "type": "string",
                                "description": (
                                    "Passbankens övningskod om den finns "
                                    "(strength_exercises.yaml), t.ex. 'back_squat'. "
                                    "Stabil nyckel för progressionshistoriken."
                                ),
                            },
                            "sets": {"type": "integer"},
                            "reps": {"type": "integer", "description": "Målreps att börja på."},
                            "reps_min": {
                                "type": "integer",
                                "description": "Repspannets golv, t.ex. 3 för maxstyrka.",
                            },
                            "reps_max": {
                                "type": "integer",
                                "description": (
                                    "Repspannets tak. Vid taket växlar progressionen "
                                    "till tyngre vikt och går tillbaka till golvet."
                                ),
                            },
                            "weight_from": {"type": "number", "description": "Startvikt i kg, om känd."},
                            "load": {
                                "type": "string",
                                "description": (
                                    "Lastanvisning, t.ex. '80% 1RM'. Skriv 'kroppsvikt' "
                                    "för kroppsviktsövningar — då progredierar reps "
                                    "i stället för kilon."
                                ),
                            },
                            "rir": {"type": "integer", "description": "Reps in reserve."},
                            "rest_sec": {"type": "integer"},
                            "note": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["date", "sport", "title"],
        },
        _tool_plan_session,
    ),
    (
        "get_session_feedback",
        "Vilka pass adepten gillat: svaren på \"Vill du göra passet igen?\" "
        "(yes/maybe/no) med ansträngning (1–10), per pass och per passtyp. "
        "Läs den innan du föreslår ett nytt pass till passbanken, och när du "
        "väljer pass ur passbanken — pass adepten vill göra igen blir gjorda.",
        {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Dygn bakåt (7–365, default 90)."}
            },
        },
        _tool_get_session_feedback,
    ),
    (
        "propose_workout",
        "Föreslå ett NYTT pass till Trixas passbank, byggt på pass adepten gjort "
        "och gillat. Förslaget granskas av en människa innan det läggs in och "
        "når någon adept — det påverkar inte planen nu. Skriv i passbankens "
        "språk: du-form, känsla före fysiologi, inga förkortningar (HR, pace, "
        "strides, RIR …), FTP/CSS bara inom parentes; inga namn eller "
        "hälsouppgifter, passbanken delas med alla. source_session_ids ska vara "
        "adeptens gjorda pass (get_session_feedback/get_week); servern räknar "
        "underlaget själv. Svaret ger förhandsvisning, underlag och varningar; "
        "fel form eller fackord avvisas med en lista över vad som ska rättas.",
        {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "enum": ["swim", "bike", "run", "strength", "brick"]},
                "category": {
                    "type": "string",
                    "description": "AE lugn uthållighet, SS snabbhet, TE tempo, ME tröskel, MF kraft, AC intervaller, T test, BW kombinationspass, ST styrka.",
                },
                "name": {"type": "string", "description": "Passets namn på svenska, t.ex. 'Lugn runda med tre backar'."},
                "intent": {"type": "string", "description": "Syftet, till adepten: vad passet ger och hur det ska kännas."},
                "main_set": {
                    "type": "array",
                    "description": (
                        "Passets delar i ordning. Varje del: segment (warmup, continuous, main, "
                        "recovery, cooldown, drills, strength_block), duration_pct (andel av "
                        "passet, summa 1.0) eller duration_min/distance_m, sets, rest_sec, "
                        "zone (1–5), effort_descriptor och description (text till adepten). "
                        "strength_block: exercise (kod ur övningskatalogen) och prescription "
                        "{sets, reps, rir, rest_sec}."
                    ),
                    "items": {"type": "object"},
                },
                "duration_min": {
                    "type": "object",
                    "description": "{default: 40, range: [25, 60]} — passets längd i minuter.",
                },
                "abort_conditions": {"type": "array", "items": {"type": "string"}},
                "equipment": {"type": "array", "items": {"type": "string"}},
                "phases": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Faser passet passar i: prep, base_1–3, build_1–2, peak, race, recovery.",
                },
                "rationale": {"type": "string", "description": "Varför passet behövs i banken, och vad adepten sagt om det."},
                "source_session_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Id för adeptens gjorda pass som förslaget bygger på.",
                },
            },
            "required": ["sport", "category", "name", "intent", "main_set", "duration_min", "source_session_ids"],
        },
        _tool_propose_workout,
    ),
    (
        "list_proposals",
        "Dina passförslag för den här adepten och hur granskningen gick "
        "(pending, accepted, rejected, imported) med granskarens kommentar.",
        {"type": "object", "properties": {}},
        _tool_list_proposals,
    ),
    (
        "delete_planned_session",
        "Ta bort ett planerat pass. Bara adeptens egna rader går att röra.",
        {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Passets id från get_week.",
                }
            },
            "required": ["session_id"],
        },
        _tool_delete_planned_session,
    ),
    (
        "log_override",
        "Dokumentera att du medvetet frångår motorns rekommendation. Krav enligt "
        "coach-praxis: vad motorn sa, vad du valde istället, och varför. Flagga "
        "om beslutet vilar på medicinsk information eller adeptens uttryckliga "
        "önskemål — motorn tar hänsyn till overriden när nästa vecka genereras. "
        "scope='week' kräver week_start, scope='workout' kräver planned_session_id.",
        {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["week", "workout", "phase", "volume", "overtraining"],
                    "description": "Vad beslutet gäller.",
                },
                "engine_recommendation": {
                    "type": "object",
                    "description": "Motorns förslag som objekt.",
                },
                "override_decision": {
                    "type": "object",
                    "description": "Ditt beslut som objekt.",
                },
                "motivation": {
                    "type": "string",
                    "description": "Motivering, minst 10 tecken.",
                },
                "medical_context_disclosed": {"type": "boolean"},
                "athlete_explicit_request": {"type": "boolean"},
                "week_start": {
                    "type": "string",
                    "description": "Veckans måndag, YYYY-MM-DD. Krävs när scope='week'.",
                },
                "planned_session_id": {
                    "type": "string",
                    "description": "Passets id från get_week. Krävs när scope='workout'.",
                },
            },
            "required": [
                "scope",
                "engine_recommendation",
                "override_decision",
                "motivation",
            ],
        },
        _tool_log_override,
    ),
]

_HANDLERS = {name: handler for name, _d, _s, handler in _TOOLS}


def _tool_descriptors() -> list[dict]:
    return [
        {"name": name, "description": desc, "inputSchema": schema}
        for name, desc, schema, _h in _TOOLS
    ]


# ---------- JSON-RPC ----------


def _result(req_id: Any, payload: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_text(payload: Any) -> dict:
    """Verktygssvar: JSON som text (bred klientkompatibilitet) + strukturerad kopia."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
    }


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _call_tool(scope: AgentScope, params: dict) -> dict:
    name = params.get("name") or ""
    args = params.get("arguments") or {}
    handler = _HANDLERS.get(name)
    if handler is None:
        # Okänt verktygsnamn är ett verktygsfel, inte ett protokollfel — modellen
        # ska få se det och kunna välja om.
        return _tool_error(f"Okänt verktyg: {name}")
    try:
        return _tool_text(handler(scope, args))
    except HTTPException as exc:
        return _tool_error(f"Trixa svarade {exc.status_code}: {exc.detail}")
    except (ValueError, TypeError, KeyError) as exc:
        return _tool_error(f"Ogiltiga argument: {exc}")
    except Exception as exc:  # noqa: BLE001 — ett trasigt verktyg får inte fälla sessionen
        logger.exception("MCP-verktyget %s kraschade", name)
        return _tool_error(f"Internt fel i {name}: {exc}")


def _dispatch(scope: AgentScope, message: dict) -> dict | None:
    """Ett JSON-RPC-meddelande → svar, eller None för notifikationer."""
    req_id = message.get("id")
    method = message.get("method") or ""
    params = message.get("params") or {}
    is_notification = "id" not in message

    if method == "initialize":
        wanted = str(params.get("protocolVersion") or "")
        return _result(
            req_id,
            {
                "protocolVersion": (
                    wanted if wanted in _SUPPORTED_PROTOCOLS else _LATEST_PROTOCOL
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Trixa är en deterministisk träningsmotor. Verktygen läser och "
                    "skriver EN adepts data — den som token:en tillhör.\n\n"
                    "Planera inom Trixas träningslära, inte utanför den. Börja med "
                    "get_engine_week: motorns förslag med fas, tillåtna passtyper och "
                    "adeptens nivågränser (längsta pass, antal pass, veckobudget). "
                    "Välj pass ur passbanken (list_workouts) och skriv dem med "
                    "workout_code — då får adepten passbankens text på vanlig "
                    "svenska. Egna pass utan kod, längre pass eller hårdare "
                    "passtyper än nivån tillåter är avsteg: säg varför med "
                    "log_override. En nybörjare ska aldrig få fler eller längre "
                    "pass än motorns förslag utan ett uttalat skäl.\n\n"
                    "Innan du planerar: läs också get_constraints (vad som går att lägga "
                    "alls) och get_athlete (mål, nivå, tröskelvärden). "
                    "get_constraints är bindande — planera aldrig i en gren som "
                    "ligger i inactive_sports eller blocked_sports, och lägg aldrig "
                    "pass på en dag i rest_days.\n\n"
                    "Läs get_recovery också. Saknar adepten kopplad klocka svarar "
                    "den med tom metrics-lista och en note — det är normalläget för "
                    "nya adepter och inget fel. Planera då på veckoram och "
                    "erfarenhetsnivå ur get_athlete, håll upprampningen försiktig "
                    "och fråga adepten hur passen kändes i stället för att läsa HRV.\n\n"
                    "Skriv pass med plan_session och dokumentera avsteg från motorn "
                    "med log_override.\n\n"
                    "Har adepten gillat ett pass som saknas i passbanken (se "
                    "get_session_feedback): föreslå det med propose_workout. Det "
                    "granskas innan någon får det."
                ),
            },
        )

    if is_notification:
        return None  # notifications/initialized, notifications/cancelled m.fl.

    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": _tool_descriptors()})
    if method == "tools/call":
        return _result(req_id, _call_tool(scope, params))
    if method == "resources/list":
        # Vi annonserar inga resurser/prompts, men klienter frågar ändå. Tomt
        # svar är vänligare än -32601 (som en del klienter loggar som fel).
        return _result(req_id, {"resources": []})
    if method == "prompts/list":
        return _result(req_id, {"prompts": []})
    return _error(req_id, _METHOD_NOT_FOUND, f"Okänd metod: {method}")


# ---------- HTTP ----------


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    """Streamable HTTP-endpoint. En POST = ett JSON-RPC-meddelande (eller en lista)."""
    try:
        scope = resolve_agent_scope(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            # Headern bär pekaren till resursmetadatan (RFC 9728) — det är den
            # en OAuth-klient följer för att hitta auktoriseringsservern.
            return JSONResponse(
                _error(None, _INVALID_REQUEST, str(exc.detail)),
                status_code=401,
                headers=exc.headers or unauthorized_headers(request),
            )
        return JSONResponse(
            _error(None, _INTERNAL_ERROR, str(exc.detail)), status_code=exc.status_code
        )

    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            _error(None, _PARSE_ERROR, "Kunde inte tolka JSON."), status_code=400
        )

    if isinstance(payload, list):
        responses = [
            r for r in (_dispatch(scope, m) for m in payload if isinstance(m, dict)) if r
        ]
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    if not isinstance(payload, dict):
        return JSONResponse(
            _error(None, _INVALID_REQUEST, "Förväntade ett JSON-objekt."), status_code=400
        )

    response = _dispatch(scope, payload)
    if response is None:
        return Response(status_code=202)  # notifikation — inget svar ska skickas
    return JSONResponse(response)


@router.get("/mcp")
def mcp_no_sse() -> Response:
    """Vi öppnar inget serverinitierat SSE-flöde. Spec:en vill ha 405 här."""
    return JSONResponse(
        _error(None, _INVALID_REQUEST, "Servern erbjuder ingen SSE-ström; använd POST."),
        status_code=405,
    )


@router.delete("/mcp")
def mcp_no_session() -> Response:
    """Stateless server — det finns ingen session att avsluta."""
    return Response(status_code=405)

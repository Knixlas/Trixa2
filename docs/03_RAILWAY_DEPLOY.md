# Railway-deploy — Trixa

Två services krävs på Railway. Båda pekar mot samma repo (Knixlas/Trixa2 eller
fork) och delar samma env-vars.

## Förutsättningar

1. Railway-konto på https://railway.com
2. GitHub-repo med Trixa2-koden (privat eller publikt)
3. Supabase-credentials (samma som ligger i lokal `.env`):
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
4. En genererad Bearer-token för API-auth (`TRIXA_API_TOKEN`)

## Steg 1 — Skapa Railway-projektet

1. https://railway.com/dashboard → New Project → Deploy from GitHub repo
2. Välj Trixa2-repot, branch `main`
3. Railway detekterar nixpacks och `requirements.txt` automatiskt

## Steg 2 — Web-service (FastAPI + UI)

Default-servicen som skapas blir webben.

**Settings:**
- Service name: `trixa-web`
- Start command: `uvicorn trixa_api.main:app --host 0.0.0.0 --port $PORT`
- Healthcheck path: `/health`
- Public networking: PÅ (generera en `*.up.railway.app`-domän eller länka egen)

**Env-vars (Variables-fliken):**
```
SUPABASE_URL=https://vtwqebihrxrufgrzmefe.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
TRIXA_API_TOKEN=<generera-en-stark-token>
TRIXA_DEFAULT_USER_ID=09db449d-b8fd-409a-b475-3401b0de9858
```

Generera token lokalt: `python -c "import secrets; print(secrets.token_urlsafe(48))"`

## Steg 3 — Worker-service (cron)

I samma Railway-projekt: Add Service → Empty service → Connect to GitHub repo
(samma repo).

**Settings:**
- Service name: `trixa-worker`
- Start command: `python -m coach.trixa.cron`
- Healthcheck: AV (worker har ingen HTTP-port)
- Public networking: AV

**Env-vars:** Samma som webben + valfritt:
```
TRIXA_CRON_HOUR_UTC=20      # default 20:00 UTC
TRIXA_CRON_WEEKDAY=6        # default söndag (0=mån)
TRIXA_CRON_POLL_SEC=3600    # poll var hour
```

## Steg 4 — Verifiera

Efter deploy (Railway visar log och status):

```bash
# Health
curl https://trixa-web-xxxx.up.railway.app/health

# Auth-gated endpoint
curl -H "Authorization: Bearer $TOKEN" \
  https://trixa-web-xxxx.up.railway.app/api/athlete/09db449d-b8fd-409a-b475-3401b0de9858
```

UI: öppna `https://trixa-web-xxxx.up.railway.app/` i webbläsaren.

Worker-loggen ska visa "Trixa-cron startad..." och poll-meddelanden.

## Steg 5 — Koppla Nils (Claude mobile) till Trixa

Två alternativ:

### A. Direkt mot Trixa-API (rekommenderat)

I Claude mobile → Settings → Connectors → Add custom MCP / HTTP-konnektor
mot din Railway-URL. Trixa exponerar OpenAPI på `/openapi.json` så Claude
kan auto-introspekta.

### B. Via Supabase MCP (snabbare för pure läsning)

Lägg till Supabase-konnektorn i Claude mobile med samma `SUPABASE_URL` +
`SUPABASE_SERVICE_ROLE_KEY`. Nils läser tabellerna direkt. Override-skrivning
ska fortfarande gå via Trixa-API:t (för validering + audit-trail).

## Felsökning

| Symptom | Trolig orsak |
|---|---|
| Build failar med pyiceberg-wheel | Python 3.14 — sätt Python 3.12 i `nixpacks.toml` |
| `Saknar SUPABASE_URL` | Env-vars sätts på fel service eller fel skift |
| Healthcheck timeout | App startar långsamt — höj `healthcheckTimeout` i `railway.toml` |
| Worker triggas aldrig | Kolla TRIXA_CRON_WEEKDAY (0=mån). Sön = 6. |
| 401 från API | Token-mismatch. Verifiera att samma token finns både lokalt och i headern. |

## Kostnadsuppskattning

Två services × shared CPU (0.1 vCPU) + ~512MB RAM = ~5-10 USD/mån.
Egen domän tillgänglig via Railway (du kopplar in DNS hos t.ex. Cloudflare).

## Nästa steg efter deploy

- Begränsa CORS `allow_origins` i `trixa_api/main.py` till din domän
- Byt `TRIXA_ALLOW_NO_AUTH` är AV i produktion (det ska aldrig vara satt)
- Sätt upp Supabase Auth + adept-JWT (ersätter Bearer-token-MVP)
- Iterativ förbättring: schemaläggning, .fit-export, fler formulär

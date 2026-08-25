# 11 — Koppla Trixa till en AI-klient (MCP)

**Syfte:** Hur en adept får sin egen AI att läsa och skriva i Trixa, utan att
någon behöver kopiera data manuellt eller ge AI:n rå databasåtkomst.

Trixa exponerar två ytor med **samma** per-adept-token:

| Yta | Protokoll | Vem använder den |
|---|---|---|
| `/agent/*` | REST + JSON | Skript, integrationer, AI:er som kan göra HTTP själva |
| `/mcp` | MCP (streamable HTTP, JSON-RPC 2.0) | AI-klienter: Claude Code, Claude Desktop, Cursor m.fl. |

Token = identitet. Alla anrop låses till **en** adept — det finns ingen
`athlete_user_id`-parameter att manipulera, så en token kan aldrig nå någon
annans data. Bara hashen lagras; råvärdet visas en gång vid skapandet.

## Steg 1 — adepten skapar en token

Logga in i Trixa → **Inställningar** → *AI-åtkomst* → namnge token:en
(t.ex. "Claude på jobbdatorn") → **Skapa token**.

Kopiera värdet direkt. Det visas aldrig igen. Tappat bort den? Återkalla och
skapa en ny — den gamla slutar fungera i samma sekund.

## Steg 2 — koppla in den i AI-klienten

### Claude Code

```bash
claude mcp add --transport http trixa https://<din-trixa-url>/mcp --header "Authorization: Bearer trixa_..."
```

Verifiera med `/mcp` i en session, eller be klienten köra verktyget `whoami` —
svarar det med rätt namn är kopplingen klar.

### Claude Desktop, Cursor och andra som läser en JSON-config

```json
{
  "mcpServers": {
    "trixa": {
      "type": "http",
      "url": "https://<din-trixa-url>/mcp",
      "headers": { "Authorization": "Bearer trixa_..." }
    }
  }
}
```

### claude.ai (webb och mobil) — fungerar INTE än

Custom connectors i claude.ai kan inte skicka en egen `Authorization`-header;
de förhandlar OAuth. Trixa har inget OAuth-lager ännu, så en adept som bara
använder claude.ai i webbläsaren eller mobilen kan inte koppla upp sig i dag.
Det är nästa etapp — se "Vad som återstår" nedan.

**Lägg aldrig token:en i URL:en** som ett vägsegment för att komma runt det.
Den läcker då i server-, proxy- och webbläsarloggar och går inte att få tillbaka.

## Verktygen

| Verktyg | Läser/skriver | Gör |
|---|---|---|
| `whoami` | läs | Vilken adept är token:en låst till? |
| `get_athlete` | läs | Mål, erfarenhetsnivå, tröskelvärden, veckoram, hälsa, fasläge |
| `get_week` | läs | Planerade pass (default: innevarande vecka) |
| `get_training_log` | läs | Genomförda pass, alla källor |
| `get_recovery` | läs | RHR, HRV mot baseline, sömn, readiness, belastningskvot |
| `plan_session` | skriv | Lägg/ändra ett pass — upsert på (datum, gren) |
| `delete_planned_session` | skriv | Ta bort ett pass |
| `log_override` | skriv | Dokumentera avsteg från motorns rekommendation |

Läsvägarna normaliserar sport till engelska (`bike`/`run`/`swim`/`strength`/
`rest`/`brick`); lagringen sker på svenska. Skrivvägen tar emot båda.

Pass som skrivs via `plan_session` får `origin='nils'` och är **skyddade** —
motorn genererar aldrig över en dag som redan har en mänskligt skapad rad.

## Vad servern gör och inte gör

- **Stateless.** Ingen session-id-hantering. Varje POST bär sin egen token.
- **Ingen SSE.** `GET /mcp` och `DELETE /mcp` svarar 405, vilket transporten
  tillåter för servrar som inte öppnar egna strömmar.
- **Inga resources eller prompts.** Bara tools. `resources/list` och
  `prompts/list` svarar tomt istället för "okänd metod", för att slippa
  falsklarm i klientloggar.
- **Verktygsfel fäller inte sessionen.** Ogiltiga argument, saknade rader och
  HTTP-fel kommer tillbaka som `isError` med begriplig text, så modellen kan
  rätta sig själv.

Implementationen ligger i `trixa_api/mcp_server.py` och är tunna omslag runt
funktionerna i `trixa_api/agent_api.py` — det finns exakt en implementation av
varje regel. Tester: `coach/tests/test_mcp_server.py`.

## Felsökning

| Symtom | Sannolik orsak |
|---|---|
| 401 på allt | Token saknas, är felkopierad, eller återkallad i Inställningar |
| `whoami` ger fel adept | Token tillhör ett annat konto — skapa en ny från rätt inloggning |
| Klienten hittar inga verktyg | Pekar på fel URL — sökvägen är `/mcp`, inte `/agent` |
| `get_recovery` ger tom lista | Ingen `garmin_athlete_id` kopplad; TP-synken har inte fyllt cachen |
| Skrivna pass syns inte i klockan | TP-pushen är avstängd — `TRIXA_PUSH_TO_TP` på workern |

## Vad som återstår

**OAuth-lagret.** För att adepter ska kunna koppla in Trixa som en custom
connector i claude.ai (webb + mobil) krävs OAuth 2.1 med Dynamic Client
Registration och PKCE framför `/mcp`:

- `/.well-known/oauth-protected-resource` och
  `/.well-known/oauth-authorization-server`
- `/register` (DCR), `/authorize`, `/token`
- inloggningen kan luta sig mot Supabase Auth som redan bär användarna

Verktygsytan i det här dokumentet ändras inte av det — OAuth byter bara ut hur
`/mcp` får veta vilken adept anropet gäller. Allt annat står kvar.

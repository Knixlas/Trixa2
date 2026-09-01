# 11 — Koppla Trixa till en AI-klient (MCP)

**Syfte:** Hur en adept får sin egen AI att läsa och skriva i Trixa, utan att
någon behöver kopiera data manuellt eller ge AI:n rå databasåtkomst.

Trixa exponerar två ytor, och två sätt att autentisera mot dem:

| Yta | Protokoll | Vem använder den |
|---|---|---|
| `/agent/*` | REST + JSON | Skript, integrationer, AI:er som kan göra HTTP själva |
| `/mcp` | MCP (streamable HTTP, JSON-RPC 2.0) | AI-klienter: claude.ai, Claude Code, Claude Desktop, Cursor |

| Autentisering | För vem |
|---|---|
| **Personlig token** som adepten klistrar in | Klienter som kan sätta egna headers (Claude Code, Desktop, Cursor) |
| **OAuth 2.1** — adepten loggar in och godkänner | Klienter som inte kan det (claude.ai i webb och mobil) |

Oavsett väg: token = identitet, alla anrop låses till **en** adept. Det finns
ingen `athlete_user_id`-parameter att manipulera, så en token kan aldrig nå
någon annans data. Bara hashar lagras.

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

### claude.ai (webb och mobil) — via OAuth, ingen token behövs

Lägg till en custom connector med adressen `https://<din-trixa-url>/mcp`.
Lämna Client ID och Client Secret **tomma** — Trixa registrerar klienten
automatiskt (DCR). Claude skickar dig till Trixa, du loggar in med ditt vanliga
konto och godkänner. Klart.

Får du en ruta som ber om Client ID betyder det att discovery misslyckades —
se Felsökning nedan.

**Lägg aldrig en token i URL:en** som ett vägsegment. Den läcker då i server-,
proxy- och webbläsarloggar och går inte att få tillbaka.

## Verktygen

| Verktyg | Läser/skriver | Gör |
|---|---|---|
| `whoami` | läs | Vilken adept är token:en låst till? |
| `get_athlete` | läs | Hela profilen: mål, nivå, tröskelvärden, veckoram, aktiva grenar, vilodagar, långpassdagar, utrustning, hälsa, fasläge |
| `get_constraints` | läs | Vad som går att planera alls — grenar, blockeringar, vilodagar, pool. Läs FÖRE du skriver pass |
| `get_week` | läs | Planerade pass (default: innevarande vecka) |
| `get_training_log` | läs | Genomförda pass, alla källor |
| `get_recovery` | läs | RHR, HRV mot baseline, sömn, readiness, belastningskvot. `has_data=false` + note när klocka saknas |
| `plan_session` | skriv | Lägg/ändra ett pass — upsert på (datum, gren). Styrkepass bär `exercises[]` som förifyller adeptens logg |
| `delete_planned_session` | skriv | Ta bort ett pass |
| `log_override` | skriv | Dokumentera avsteg från motorns rekommendation |

Läsvägarna normaliserar sport till engelska (`bike`/`run`/`swim`/`strength`/
`rest`/`brick`); lagringen sker på svenska. Skrivvägen tar emot båda.

Pass som skrivs via `plan_session` får `origin='nils'` och är **skyddade** —
motorn genererar aldrig över en dag som redan har en mänskligt skapad rad.

## OAuth-kedjan

Vad klienten faktiskt gör, i ordning:

```
1. POST /mcp utan token       → 401 + WWW-Authenticate: Bearer resource_metadata="…"
2. GET  /.well-known/oauth-protected-resource      (RFC 9728) → var ligger auth-servern?
3. GET  /.well-known/oauth-authorization-server    (RFC 8414) → vilka endpoints finns?
4. POST /oauth/register       (RFC 7591)  → client_id, utan handpåläggning
5. GET  /oauth/authorize      → adepten loggar in och godkänner
6. POST /oauth/token          → access + refresh
7. POST /mcp med Bearer       → verktygen
```

**Steg 1 är det som brukar fälla integrationer.** Utan `resource_metadata` i
401-svaret har klienten ingenstans att börja leta, och faller tillbaka på att
fråga användaren om ett Client ID som ingen kan svara på.

### Säkerhetsegenskaper

- **PKCE S256 krävs.** `code_challenge_methods_supported: ["S256"]` finns i
  AS-metadatan — saknas den vägrar klienter ansluta även om allt annat stämmer.
- **Public clients.** Ingen `client_secret` utfärdas och ingen behövs. En secret
  ska aldrig skickas över chatt eller mejl.
- **Exakt redirect-matchning.** En `redirect_uri` som inte registrerats leder
  till ett felmeddelande *på Trixa*, aldrig till en vidarebefordran — annars
  vore det en öppen redirect.
- **Audience-bindning (RFC 8707).** Token bär vilken resurs den gäller för;
  `/mcp` avvisar tokens utfärdade för någon annan.
- **Rotation.** Access-token lever 1 h, refresh 30 dygn och roteras vid varje
  användning. Återanvänd kod eller refresh → hela kedjan för (app, adept)
  återkallas.
- **Adepten kan dra tillbaka.** Inställningar → *Kopplade appar*. Verkar direkt.

Öppen registrering är vad specen rekommenderar för MCP. Att registrera sig ger
ingen åtkomst — adepten måste ändå logga in och godkänna.

**CIMD** (`client_id_metadata_document_supported`) är inte implementerat. DCR
räcker för Claude, och CIMD kräver att servern hämtar ett dokument från en
adress klienten anger — en SSRF-yta som inte är värd det förrän någon klient
behöver den.

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
| `get_recovery` ger tom lista | Normalt utan kopplad klocka — svaret bär `has_data: false` och en note. Med klocka: TP-synken har inte fyllt cachen |
| Agenten planerar i fel gren | Den läste inte `get_constraints`. Den är bindande: `inactive_sports`, `blocked_sports` och `rest_days` är hårda gränser |
| Skrivna pass syns inte i klockan | TP-pushen är avstängd — `TRIXA_PUSH_TO_TP` på workern |
| Connectorn frågar efter Client ID | Discovery nådde inte fram — se nedan |
| Connectorn kopplar upp mot fel adress | `TRIXA_PUBLIC_URL` saknas eller pekar fel |

### När connectorn frågar efter Client ID

Det betyder att klienten inte kunde följa discovery-kedjan. Gå igenom stegen
i tur och ordning — felet ligger alltid i det första som inte svarar rätt:

```bash
curl -i -X POST https://<url>/mcp -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Svaret ska vara `401` med `WWW-Authenticate: Bearer resource_metadata="…"`.

```bash
curl https://<url>/.well-known/oauth-protected-resource
curl https://<url>/.well-known/oauth-authorization-server
```

Det andra svaret **måste** innehålla `"code_challenge_methods_supported":
["S256"]` och `"none"` i `token_endpoint_auth_methods_supported`.

Adresserna i metadatan måste vara exakt de klienten når servern på — `http`
i stället för `https`, eller ett internt värdnamn, får klienten att avbryta.
Det styrs av `TRIXA_PUBLIC_URL`.

## Vad som återstår

- **CIMD** om någon klient börjar kräva det (se resonemanget ovan).
- **Utgångna koder och tokens** städas inte automatiskt. Raderna är små och
  döda, men en `delete from oauth_authorization_codes where expires_at < now()`
  då och då skadar inte.

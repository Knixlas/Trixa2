# Trixa2

Logikstyrd triathloncoach med kod (ej AI). Bygger på data från Garmin Connect, lagras i Supabase, och triggas via GitHub Actions.

## Struktur

- [`garmin-mcp/`](./garmin-mcp/) – MCP-server + sync-lager mot Garmin Connect. Se [garmin-mcp/README.md](./garmin-mcp/README.md) för detaljer.
- [`.github/workflows/sync.yml`](./.github/workflows/sync.yml) – schemalagd och manuell sync via GitHub Actions.

## Snabbstart

1. Klona repot lokalt
2. Följ setup i `garmin-mcp/README.md`
3. Lägg in secrets enligt instruktioner och börja köra sync från mobilen via GitHub Actions

## Roadmap

- ✅ Garmin → Supabase sync
- ⏳ Coach-lager: regelbaserade träningsförslag
- ⏳ Strukturerade träningsplaner med race-mål och periodisering

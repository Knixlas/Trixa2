# Next-thread prompt: Plan vs Actual i veckovyn

Klistra in detta i en ny Claude-tråd (Opus 4.8) som första meddelande.
CLAUDE.md i repo-roten läses automatiskt så instruktionen behöver inte upprepa basen.

---

## Uppgift

Implementera "Alternativ C" från tidigare designdiskussion (se chat-history i Trixa-projektet om du har access — annars räcker beskrivningen nedan).

I dashboarden (`/ui/`) ska varje pass visa **plan + actual sida vid sida**, med statusbadge baserat på datum + Garmin-aktivitet. Plan-titeln är ALLTID synlig — adept ska se kontrasten "skulle ha kört X, körde Y".

## Status-modell per pass

Räknas ut från `w.date` vs idag + matchande aktivitet i `garmin_coach.activities`:

| Status | Villkor | Visual |
|---|---|---|
| Genomförd som planerat | Passerat datum, aktivitet finns, sport matchar, duration inom ±30% | 🟢 grön badge |
| Avviken | Passerat datum, aktivitet finns men annan disciplin ELLER duration ±30% utanför | 🟡 gul badge |
| Missad | Passerat datum, ingen matchande aktivitet | 🔴 röd badge |
| Planerad | Framtida datum | 🔵 blå badge |
| Idag | `w.date == today` | ⚪ grå badge |

## Bygg i två steg, en commit per steg

### Steg 1: Status-badge per pass (~45 min)
- Hämta `garmin_coach.activities` för veckan i `_fetch_current_week_data` (filen `trixa_api/ui.py`)
- Mappa aktivitetstyper: `running`→`run`, `cycling`→`bike`, `swimming`/`lap_swimming`/`open_water_swimming`→`swim`, `strength_training`→`strength`
- För varje pass: räkna ut status baserat på `w.date` vs idag + matchande aktivitet
- Visa badge i `trixa_api/templates/_week_section.html` per pass
- Pusha (separat commit)

### Steg 2: Actual-data under passerade pass (~45 min)
- För 🟢/🟡-pass: visa duration_sec, hr_avg, training_load, distance under plan-titeln
- Format: "Genomfört: 52 min, 145W avg, TSS 67"
- Klick → expand för fullständig aktivitetsdata
- Pusha (separat commit)

## Filer som ska ändras

- `trixa_api/ui.py` — `_fetch_current_week_data` utökas med activities-läsning + matchningslogik
- `trixa_api/templates/_week_section.html` — visa status-badge + actual-rad

## Datakontrakt: garmin_coach.activities

Niklas garmin_athlete_id: `98057fa1-4fb9-48f5-be86-b31272dcfed0`

Schema (relevanta kolumner):
- `id` uuid
- `athlete_id` uuid
- `start_time` timestamptz
- `duration_sec` integer
- `activity_type` text (normaliserad: `running`/`cycling`/`swimming`/etc)
- `training_load` numeric
- `hr_avg`, `hr_max` integer
- `distance_meters` numeric
- `normalized_power` integer (för cykel)
- `hr_zones_time` jsonb (tid per zon)
- `training_effect_aerobic`, `training_effect_anaerobic` numeric

## Tolerans-regler för matching

- **Sport-matching**: planerat sport → activity_type ska mappa till samma
- **Brick-undantag**: `BAE*/BTE*/BSS*`-koder (brick = bike+run) matchar både `cycling` OCH `running`
- **Duration-tolerans**: planerat ±30% = genomfört. Mer avvikelse = avviken status
- **Sport-mismatch utan brick-undantag**: avviken
- **Multipla aktiviteter samma dag**: matcha den med närmast duration till plan
- **Vilodag (sport='rest')**: 🟢 om INGEN aktivitet finns den dagen, 🟡 om aktivitet finns (= du tränade på vilodag)

## Verifiera med live-data

En aktivitet du kan testa matching mot: "Stockholm Löpning (running) – 2026-05-27 18:58:54"-datapunkten i `garmin_coach.activities` ska matcha planerat run-pass 2026-05-27 om sådant finns.

Niklas senaste sync gjordes 2026-05-28 12:13 (success). Activities-tabellen är aktuell.

## Designprincip (viktig)

**Plan-titeln är ALLTID synlig** — även när aktivitet finns. Adept ska se kontrasten "skulle ha kört X (planerat), körde Y (faktiskt)". Det är värdefullt för retrospektiv coaching och bygger på Trixas filosofi om spårbarhet utan tolkning.

## Vad som redan är klart (rör inte)

- Engine + passbank + planner + alerts + override-protokoll
- Garmin-sync med Supabase token-store (`garmin_coach.oauth_tokens`)
- Trixa-app på Railway, FastAPI + Jinja
- Dashboard visar denna vecka + nästa vecka med edit-knappar
- Strukturerad skaderapport per disciplin (impact: none/partial/full)
- Settings-vyn för adept-prefs (dagar, utrustning, inomhus/utomhus)

Ändra bara UI-skiktet. Engine och planner behöver inte röras.

## Bonus (steg 3, senare iteration — inte i denna tråd)

När plan-vs-actual finns synlig, kan planner i framtiden läsa "vad gjorde jag förra veckan" och justera kommande veckor baserat på faktiskt utfört arbete (inte bara deklarerat). Men det är scope för en annan tråd — bygg bara UI-delarna nu.

## Live-URL för verifiering

https://trixa2.up.railway.app/ui/

Dashboarden visar denna + nästa vecka. Efter dina ändringar ska passerade dagar ha statusbadge + (steg 2) actual-data under plan-titeln.

## Tråd-praxis

- Skriv commits till `trixa-app-skeleton`-branchen
- Cherry-pick till `main` om relevant för deploy (Railway läser main? eller trixa-app-skeleton? — verifiera med git remote-head innan push)
- Inga LLM-anrop i koden — ren deterministisk kod

# Nybörjarresan — sju adepter från registrering till lopp (2026-11-22)

**Bakgrund:** En nybörjare utan träningserfarenhet registrerade sig med
Midnattsloppet 10 km som mål. Hon fick långa pass och förkortningar och
begrepp hon inte förstod.

**Metod:** `generate_week` kördes vecka för vecka mot testfejken
(`coach/tests/test_agent_api._C`), från 2026-11-23 fram till respektive
lopp. Ingen skarp databas rördes. Adepten "gjorde" 70–95 % av passen varje
vecka (de blev `training_log`-rader), så att faktisk volym matade nästa
vecka som i drift. Skriptet ligger utanför repot. Sju personer:

| # | Person | Angivet | Lopp |
|---|---|---|---|
| 1 | Nybörjare utan erfarenhet, lät förifyllda timmar stå | beginner, run, 6 h | 10 km, 38 v |
| 2 | Nybörjare som skrev 3 h | beginner, run, 3 h | 10 km, 38 v |
| 3 | Nybörjare 62 år, hälsa, ont i knät (2), 2:1 | beginner, run, 3 h | inget |
| 4 | Comeback efter två år | intermediate, run, 4 h | 10 km, 38 v |
| 5 | Van löpare med testvärden | advanced, run, 7 h | halvmaraton, 26 v |
| 6 | Triathlonnybörjare | beginner, sim+cykel+löp, 5 h | sprint, 30 v |
| 7 | Motionscyklist utan effektmätare | intermediate, bike, 6 h | Vätternrundan, 29 v |

## Vad som kom ut

Planerade timmar per vecka (vecka 1 → sista):

| # | Vecka 1 | Spann | Längsta pass |
|---|---|---|---|
| 1 | 5,7 h: 50, 75, 80, 75, 60 min löpning | 4,4–6,2 | 115 min (målfartsblock) |
| 2 | 5,3 h: 30, 75, 80, 75, 60 min | 4,3–6,1 | 115 min |
| 3 | 5,7 h: 30, 75, 80, 75, 80 min | 4,4–6,0 | 100 min |
| 4 | 5,9 h | 4,6–6,1 | 115 min |
| 5 | 6,0 h (bad om 7) | 4,3–6,1 | 115 min |
| 6 | 5,2 h | 4,1–8,7 | 158 min brick i februari |
| 7 | 6,2 h | 5,5–8,1 | 200 min |

Nybörjaren, comebacken och den vane löparen fick **samma pass** vecka 1.

## Fynd, allvarligast först

### 1. Nivå och mål läses aldrig av planeraren
`experience_level` och `goal` sparas i onboardingen men förekommer inte
någonstans under `coach/`. En adept som aldrig sprungit planeras som en
erfaren. Dessutom är "Medel" förvalt, eftersom `_ensure_athlete_profile`
skapar raden med `experience_level = intermediate` och mallen väljer det
som står i raden: nybörjaren måste själv byta.

### 2. Femtimmarsgolvet gör att de angivna timmarna inte gäller
`MIN_PLANNED_WEEKLY_HOURS = 5.0` (`planner.py:550`). Den som skriver 3 h
får 5,3. Golvet tillämpas också på faktisk historik, så en nybörjare som
gör 70 % hålls kvar vid golvet i stället för att planen anpassas. För en
nybörjare är 5 h löpning i vecka 1 en skaderisk, och inte en
utgångspunkt. Den vane löparen får samtidigt *mindre* än de 7 h hen bad
om, eftersom faktisk volym vinner över deklarerad.

### 3. Passlängderna utgår från passbankens standardvärden, inte från adepten
"Nio minuter löpa, en minut gå" har 75 min som standard och kommer två
gånger samma vecka. "Lugn skogsrunda" 80 min. Det finns inget tak per pass
som följer adeptens nivå eller loppets längd. En 10 km-nybörjare får
115 min långpass med målfartsblock och "Testlopp: 2×8 min allt du har".

### 4. Loppveckan trappas inte ner
`_optimal_phase_for_race` ger fasen `race` när det är ≤ 2 veckor kvar. Den
fasen har ingen volymfaktor i `_apply_week_volume_scaling` (bara peak,
transition och vilovecka skalas), så de **tre sista veckorna går på full
volym**. Veckan före Midnattsloppet: 75, 80 och 75 min onsdag–fredag, inget
pass på loppdagen och en lätt jogg dagen efter. Det drabbar alla adepter,
inte bara nybörjare.

### 5. Utvecklarens text visas för adepten
`render_workout` skriver ut allt i passet:
- metaraden `*SS2_run_template · SS · faser: prep, base_1, …*`
- `**Parameteriserat pass** — duration 30–70 min (default 50)`
- `**Tränarens noter:**`, som är skrivna till utvecklaren ("Renderaren plockar
  fram pickup-schema …", "Den här mallen är 80 % av all löpning i
  base-fasen").

### 6. Zonerna förklaras aldrig, och utan testvärden blir de bara "Z2"
`_fmt_zone_target` returnerar `Z2` när värden saknas. Nybörjare får aldrig
se testvärdesfälten, så hos dem står det alltid bara `@ Z1`, `@ Z2`. Ingen
text säger vad zonerna betyder i känsla ("du kan prata i hela meningar").
Avbrottsvillkoren bygger på puls som adepten inte har: "HR driftar över Z2".

### 7. Fackspråket i passtexterna
Räknat över nybörjarens 38 veckor: Z1/Z2 cirka 1 100 gånger, `@` 486,
HR 211, aerob/anaerob 121, pace 60, strides 73, intervallform 61, reps 46,
fartlek 15, tröskel 14. Dessutom mitokondrier, kardiovaskulär drift,
pickups, IM-maratonstrategi, "base-fasen". Kategoribrickan visar
förkortningen (AE, SS, MF) när den saknar etikett. Cyklisten utan
effektmätare får FTP i texten nästan 100 gånger.

### 8. Besvär påverkar inte planen om ingen fyllt i `impact_per_discipline`
Onboardingen skriver `impact_per_discipline: {}`. Ett knä med svårighetsgrad
2 ger därför ingenting, och först grad 3 påverkar fasvalet. Den 62-årige
fick 5,7 h löpning vecka 1.

### 9. Varningar på motorspråk
"Inget pass i passbanken matchar ME + run + base_1" kan visas för adepten.

### 9b. Dashboarden (sedd i skarp miljö, inloggad som Niklas)
- Varningsrutan visar "Veckoplan klar — Passen ligger i planned_sessions
  (origin='trixa2') och engine-besluten i planens engine_decisions. Override
  kan göras av coach via API." (`alerts.yaml:144`, `plan_generated`),
  en gång per genererad vecka, så den står ett tiotal gånger i rad.
- Grenen står på engelska med versaler ("REST", "BIKE", "SWIM";
  `_week_section.html:74` skriver `w.sport` rått), och vilodagar får en
  zon ("0 min · Z2").
- "Mål: ironman" visar det råa värdet, och "14.0h" har decimalpunkt.

### 10. Triathlonnybörjaren och cyklisten
Triathlonnybörjaren får "16 snabba 25:or" och drills redan vecka 1, upp
till 8,7 h i en vecka trots att hen angav 5, och en 158 min brick i februari.
Cyklisten får 200 min pass och 8 h-veckor från 6 angivna.

## Förslag

1. **Låt nivån styra.** Inför en profil per `experience_level` i YAML:
   startvolym, veckoökning (högst ~10 %), tak per pass och gren, och vilka
   kategorier som är tillåtna. En nybörjare: 3 pass i veckan, 20–30 min,
   gå/spring; inga test- eller målfartsblock förrän en bas finns.
2. **Byt golvet mot nivåns startvolym**, och låt deklarerade timmar vara ett
   tak för nybörjare. Låt faktisk volym bara höja planen när den är högre än
   angiven för den som är van.
3. **Passlängd efter loppet:** ett långpass för 10 km behöver inte vara
   längre än cirka 1,5 × förväntad lopptid.
4. **Trappa ner loppveckan för alla:** `race` behöver en volymfaktor, och
   loppet ska stå i planen på sin dag.
5. **Dela upp passtexten** i adepttext och tränartext. Metarad,
   "Parameteriserat pass" och "Tränarens noter" ska bara visas för coachen.
6. **Zoner i klartext:** när värden saknas, skriv känslan ("lugnt, du kan
   prata") och ha en förklaring av zonerna nära första passet. Avbrottsvillkor
   ska ha en variant utan pulsklocka.
7. **En ordlista** för de ord som blir kvar, och ett lint-test som stoppar
   förkortningar i adepttext.
8. **Förval "Nybörjare"** i stället för "Medel", eller inget förval alls.

## Andra läsning: ChatGPT Pro i de sju personernas roller

ChatGPT fick personerna och siffrorna ovan i egna ord (inga namn, ingen
hälsodata, ingen kod) och fick spela var och en från registrering till
lopp. Kort sammanfattning:

- **Samma ordning på allvaret:** startdosen först, sedan loppdagen och
  tidsbudgeten. Det föreslår 3 × 25 min gång/jogg (1 h 15 min) som vecka 1
  för nybörjaren och comebacken, 3 × 15 min promenad för 62-åringen och
  2 h 55 min för triathlonnybörjaren.
- **Tre saker som saknades i min lista:**
  1. Fråga per gren om de **senaste fyra veckorna** och det **längsta
     bekväma passet**, i stället för en nivå som förvalts till "Medel".
     Startdosen ska komma ur svaren.
  2. **Fråga varför ett pass uteblev** (tid, för tungt, besvär) och låt
     svaret styra om nästa vecka upprepas, lättas eller byggs vidare.
     Följsamhet i procent räcker inte.
  3. Visa **"Planerat 1 h 15 min av dina 3 h"** och märk vilket pass som
     ska behållas om veckan spricker. Ställ frågan som "hur mycket tid kan
     du som mest lägga".
- **Loppet ska ligga i kalendern först**, och "lopp på anmält datum" bör
  vara ett test för varje distans.
- **Registreringen:** visa först frågorna som avgör nästa vecka, och låt
  coachnamnet vänta tills första planen syns. Skriv 3:1 som "tre vanliga
  veckor och en lättare".
- **Zonerna i känsla** (samtalstestet), samma idé som nu ligger i
  renderaren.

## Steg 1: passtexten (PR #57, mergad 2026-11-22)

- `render_workout` visar inte längre passkod, kategori, fasnamn, mallens
  spann eller "Tränarens noter" för adepten (`for_coach=False` är
  standard). `verify_and_render` begär den fulla texten.
- Zoner utan testvärden skrivs i känsla: "Z2 – lugnt, du kan prata i hela
  meningar". `@` har blivit "i".
- Segmentrubriker på svenska: Huvuddel, Teknikövningar, Bara benspark,
  Bara armtag, Stegring, Spurt. Cykelns rad: inomhus/utomhus, "kräver
  cykeltrainer".
- `plan_generated`-varningen säger "Veckans pass är klara. Du hittar dem
  under Veckans träning."
- Dashboarden visar grenen på svenska (`sport_sv`-filtret).

## Steg 2: planeraren efter nivå (grenen `planeraren-niva`)

Databasen visade att kollegan faktiskt valt **Nybörjare** och angett
**0,5 h/vecka**. Hon fick ändå fem pass i veckan: golvet på 5 h och regeln
"minst fem pass" körde över både nivå och tid.

**Nivåprofiler** i `coach/data/levels.yaml`, läses av `coach/trixa/levels.py`:

| Nivå | Modell | Start | Pass/vecka | Längsta pass (löp) | Kategorier |
|---|---|---|---|---|---|
| Nybörjare | ramp | 75 min | högst 3 | 75 min | bara AE; SS efter 8 gjorda veckor |
| Medel | ramp | 150 min (eller loggens snitt) | högst 4 | 120 min | alla |
| Avancerad, elit | declared (som förut) | — | minst 5 | fasens tak | alla |

- **Ramp:** budgeten växer 10 % när förra veckan blev gjord (≥ 75 % av
  schemalagd tid), står still vid 50–75 % och sjunker 10 % under 50 %. Den
  växer från det som faktiskt schemalades, inte från en budget passtaken
  aldrig lät veckan nå. Lugna veckor flyttar inte basen. **Angivna timmar
  är ett tak**; ligger taket under nivåns startdos säger planen det till
  adepten. Läget sparas i `phase_state.ramp`.
- **Gå och jogga** (`AE1_run_gajogg`, bara nybörjare): nybörjarens enda
  löppass tills `run_walk_steps` är slut. 1 min jogg / 1,5 min gång i
  början, ett steg upp varannan gjord vecka, upp till 10 / 1.
- **Längsta pass** för ramp-nivåerna: nivåns tak och loppets
  (`race_long_cap_min`, t.ex. 90 min för 10 km).
- **Budgetanpassningen** (`_fit_week_to_budget`): för många pass → det
  minst värdefulla blir vila (långpasset och grenens enda pass sist);
  mallpass med tidsparameter skalas mot budgeten. Styrkan räknas in i
  budgeten för ramp-nivåerna och går först om den inte ryms.
- **Advanced/elit:** volymen beräknas som förut. Nytt är att vilovecka,
  topp, nedtrappning och transition faktiskt kortar passen. Förut påverkade
  faktorn bara långpassets tak. Antalet pass står kvar.
- **Loppet** (alla nivåer): fasen `race` (≤ 2 v kvar) trappar ner till 80,
  60 och 45 % (`race_taper`). Loppet läggs in på sin dag ("Loppet: …",
  kategori RACE) med vila dagen före och dagen efter. Dagar coachen har
  reserverat rörs inte.
- **Registreringen:** nivån har inget förval före första sparningen, och
  tidsfrågan lyder "Hur mycket tid kan du som mest lägga …", med en mening
  om att det är ett tak. Belastningscykeln skrivs i ord.

Simuleringen efter ändringen (samma sju personer plus kollegans verkliga
svar; varje pass görs till 85–95 % av tiden):

| Person | Vecka 1 | Längsta pass | Loppveckan |
|---|---|---|---|
| Kollegans svar (nybörjare, 0,5 h, löp+styrka) | 1 × 30 min gå/jogg | 30 min | 30 min + loppet |
| Nybörjare, 6 h förifyllt | 3 × 20–30 min gå/jogg (1,2 h) | 75 min | 30 + 60 min + loppet |
| Nybörjare, 3 h | samma; når taket 3 h vecka 17 | 75 min | 30 + 50 min + loppet |
| 62-åring, hälsa | 3 × gå/jogg (1,2 h) | 70 min | — |
| Comeback, medel, 4 h | 4 pass, 2,4 h | 75 min | 30 + 40 + 40 min + loppet |
| Van löpare, avancerad | 6,0 h som förut | — | 30 + 25 + 25 min + loppet |
| Triathlonnybörjare | 1,3 h: sim 20, cykel 45, gå/jogg 15 | 100 min (loppet) | — |
| Motionscyklist, medel | 2,5 h → 6 h (taket) | 240 min | 65 + 65 + 30 min + loppet |

## Kvar

- ~~Besvär utan `impact_per_discipline`~~ — gjort i steg 4 nedan.
- ~~Passbankens prosa~~ — gjort i steg 3 nedan.
- **Varför blev passet inte gjort?** (ChatGPT:s förslag) Rampen ser bara
  tiden. En adept som gör två av tre pass står still utan att få veta varför.
- ~~Fasnamnet~~ — gjort i steg 5 nedan.
- **Simpassen** för triathlonnybörjaren har fortfarande drills och "25:or".

## Steg 3: passbankens språk (grenen `passbank-sprak`)

- Alla fält adepten ser i 127 pass, 27 teknikövningar och styrkekatalogens
  namn omskrivna efter `docs/PASSBANK_SPRAK.md`: du-form, känsla före
  fysiologi, FTP/CSS bara inom parentes, svenska namn ("Grundstyrka — hela
  kroppen", "Korta spurter — 16 × 25 m med gott om vila", "Benspark på sidan").
  Tränarnoter och katalogens tränarfält är orörda.
- `coach/tests/test_passbank_sprak.py` stoppar 38 ord och koder i
  adepttexten; `test_adepttext.py` renderar hela banken och kontrollerar
  slutresultatet, renderarens egna ord inräknade. 555 träffar före, 0 efter.
- Renderaren: utrustning på svenska, "trampfrekvens … varv/min", "låst
  effekt på trainern", "3 omgångar à 10, orka 2 till, vila 60 s",
  hålltider utan "2×1", "4 × 8 min i Z4 …, vila 4 min mellan".
- Tio styrkeövningar bytte namn; loggar under de gamla namnen hittas via
  `_FORMER_CATALOG_NAMES` i `strength_progression.py`. Prosatolken läser
  både "RIR 2"/"3×10" och "orka 2 till"/"3 omgångar à 10".

## Steg 4: besvär påverkar planen (grenen `besvar-paverkar`)

- `coach/data/injury_impact.yaml` + `coach/trixa/injuries.py`: saknad eller
  "auto"-påverkan bedöms ur kroppsdel och grad. Löpning belastar knä, höft,
  vad, hälsena, fot hårt; simning och styrka axel och armar; ländrygg är
  medel för de flesta. Grad 2 på en hårt belastad kroppsdel → bara lugna
  pass i grenen; grad 4 → grenen vilar. Uttryckligt val gäller alltid.
- Planeraren, rampen och MCP (`get_constraints`) använder samma bedömning.
  För nybörjare och medel står budgeten och gå/jogg-stegen still så länge
  besväret påverkar en aktiv gren.
- Adepten får en mening om vad besväret gör ("Du har angett ont i knät
  (grad 2). Trixa ger bara lugna pass i löpning och ökar inte mängden förrän
  det är bättre."). Övriga skadevarningar är omskrivna från "impact=full".
- Hälsosidan: förvalet är "Låt Trixa bedöma"; svenska ord i stället för
  none/partial/full och "Severity".
- **Befintliga besvär** som sparats via hälsosidan med "none" i alla grenar
  lämnas som de är — det går inte att skilja ett val från det gamla
  förvalet. Besvär från onboardingen (`{}`) bedöms nu automatiskt; det kan
  ändra planen för befintliga adepter med sådana besvär.
- 62-åringen med knä grad 2 i simuleringen: ligger kvar på 1,2 h med gå/jogg
  i stället för att växa till 2,3 h.

## Steg 5: fasen följer den egna upptrappningen (grenen `fasnamn`)

- Bas krävde 5 h/vecka och uppbyggnad 7 h (phases.yaml), så nybörjare och
  medel stod i "Förberedelse" tills toppningen. Nu följer deras fas antalet
  gjorda veckor (`levels.yaml::phase_by_progress_weeks`): nybörjaren går
  till grundträning efter 4 och uppbyggnad efter 12 gjorda veckor, medel
  börjar i grundträning och bygger efter 6. Loppets tidpunkt sätter
  fortfarande taket, och skada eller överträning backar till förberedelse.
- Samma bedömning i planeraren, översikten och passbytena
  (`AthleteState.sustainable_phase_override`, satt i `_build_athlete_state`).
- Kollegans 10 km i simuleringen: förberedelse till 24 v före loppet,
  grundträning, uppbyggnad 12 v före, toppning 4 v före, tävling 2 v före.
- Översikten: fasen på svenska ("Aktuell fas: Grundträning"), målet och
  distansen i ord, "Mest tid per vecka: 14 h". För nybörjare och medel
  står "bygger grunden i din takt" i stället för "⚠ ligger efter".

## Vad som inte är verifierat

- Simuleringen kördes mot testfejken, inte mot skarp data, och utan TP- eller
  Strava-data. Med en klocka kan återhämtningssignalerna sänka volymen något.
- Torrkörningen mot skarp data (`python -m coach.trixa.planner
  --athlete-user-id … --week-start …` utan `--apply`) är inte gjord för steg 2.
- Kollegans pass har inte lästs. Nivå och angiven tid kommer ur
  `athlete_profiles`.

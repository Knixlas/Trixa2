# Cykelpassbank — schema

Samma grundschema som sim- och löpbankerna. Cykel-specifika tillägg
och avvikelser dokumenteras nedan.

## Zon-system

Refererar `coach/engine/zones.py`. För cykel returnerar zones.py två
parallella zon-set från samma tröskelnivå:

- `cycling_power` — watt-spann per zon, beräknat från FTP
- `cycling_hr` — bpm-spann per zon, beräknat från LTHR (Lactate Threshold
  HR för cykel — separat från löp-AT)

Renderaren slår upp `zone: N` mot **båda** och formaterar t.ex.:

  > Z3: 150–178 W, 145–158 bpm

**Watt är primärt target, puls är sekundärt.** En watt är en watt oavsett
backe, vind eller underlag — pulsen påverkas av värme, dehydrering och
dagsform. Båda visas; vid avvikelse vinner watt om passet är tröskelträning
eller högre, puls vinner om passet är aerobt och pulsen är ovanligt hög
(varningssignal).

### Prereq i zones.py (öppet spår)

`cycling_hr_zones()` behöver läggas till i zones.py — idag finns bara
`cycling_zones()` → watt. Kräver också `lthr_bike` i `athlete_profile`
(separat från `at_hr` som är löp-tröskel). Tills detta är på plats
renderar passet bara watt + segmentbeskrivning, analogt med hur run
hanterar saknad `threshold_pace_sec_per_km`.

## Cykel-specifika fält

### `setting`
Workout-level. `outdoor | indoor | either`.

- `outdoor` — pass som typiskt körs ute (AE2 långpass, MF2 Hilly Ride)
- `indoor` — pass som typiskt körs på trainer (ME-pass, AC-pass där kontroll
  är värd mer än miljöombytet)
- `either` — fungerar likvärdigt båda (AE1 Recovery, SS1 Spin-ups)

Ersätter run-schemats `surface`. För cykel är vägbeläggning sällan
beslutsfattande — det är trainer-vs-ute som är skiljelinjen.

### `requires_trainer: true`
Striktare än `setting: indoor`. Passet **måste** köras på trainer för att
fungera (snabba target-skiften som kräver ERG, eller helt platta intervaller
som blir omöjliga med trafik/backar). Sätts på ME3 Crisscross, AC1 30/30,
korta VO2-intervaller.

### `outdoor_only: true`
Passet kräver en specifik utomhusmiljö som inte kan emuleras inomhus.
MF3 Hill Repeats — kräver riktig backe av rätt längd och lutning. MF2 Hilly
Ride — kräver kuperad terräng.

### `erg_mode`
Workout-level default + segment-level override. Bool.

- Workout-level `erg_mode: true` → renderaren genererar .fit med fasta
  watt-targets, ingen ramp. Trainern reglerar motståndet automatiskt.
- Segment-level `erg_mode: false` → override för specifikt segment (vanligt
  för warmup/cooldown där renderaren genererar fri-ride-segment istället).

Default på trainer-pass där precision betyder något (ME, AC). Hoppas över
på outdoor-pass där det inte är aktuellt.

### `cadence_rpm`
Segment-level. `[low, high]` eller ensam siffra.

Eget fält — **inte** inbakad i `effort_descriptor`. Renderaren formaterar
konsekvent ("Kadens: 100–110 rpm") jämte watt/HR-target. Används för:

- SS1 Spin-ups: höga värden (95–115 rpm) — kadens är *target*, watt är
  bara "håll lätt"
- SS2 Isolated Leg: moderat (80–90 rpm) — fokus på jämn pedaltramp
- MF1 Force Reps: låga värden (50–60 rpm) — låg kadens + hög watt = styrka

Om `cadence_rpm` saknas → ingen kadens-target, adepten cyklar naturlig
kadens (typiskt 85–95 rpm för uthållighet, 90–100 rpm för intervaller).

### `effort_descriptor`
Används istället för `zone` på segment där zonbegreppet inte applicerar väl:

- SS1 Spin-ups — för korta för HR att hinna ikapp; watt-target är fel
  fokus (kadens är poängen)
- AC1 30/30:or — HR aldrig stabilt; watt-target är reasonable men
  effort-descriptor kompletterar
- Öppnare och closers — dynamisk progression
- Race-pace-segment som inte mappar rent till en zon

Renderaren skriver ut descriptorn ordagrant utan att slå upp värden.

### `pattern` (crisscross / over-under)
Segment-level. Lista delsteg som **växlar inom repet** — för crisscross
(1 min Z4 / 1 min Z3) och over-under (2 min hög Z4 / 4 min nedre Z4). Varje
delsteg: `{ duration_min|duration_sec, zone|pct }`. Använd `pct: [lo, hi]` när
två delsteg ligger i samma zon men ska skilja sig (over-under: 103 % vs 99 %
är båda Z4); annars `zone: N`.

Två former:
- **block** — segmentet har `duration_min` (blocklängd): varje `set` är ett block
  som fylls med så många pattern-cykler som ryms, `rest_sec` läggs mellan blocken
  (ME3 Crisscross: `sets: 3` + `duration_min: 10` + 1/1-pattern → 3 block à 5 cykler).
- **kontinuerligt** — ingen blocklängd: `sets` = antal cykler i rad utan vila
  (run ME3: `sets: 6` + 2/1-pattern → 6 raka cykler).

`effort_descriptor` kan stå kvar som prosa jämte `pattern`; mappningen läser
`pattern` (exakt struktur till TP), renderaren/Nils läser texten. TP-mappningen
expanderar pattern till växlande delsteg — utan det approximeras passet till en
enda representativ zon.

### `pace_unreliable`
**Gäller inte cykel.** Watt är alltid reliabelt. Fältet finns inte i
cykelpassen.

## Schema (sammanfattning)

```yaml
workouts:
  - code: <unik kod, t.ex. AE2_bike_01>
    discipline: bike
    category: <AE|TE|MF|SS|ME|AC|T>
    type_code: <AE1|AE2|TE1|MF1|MF2|MF3|SS1|SS2|ME1|ME2|ME3|ME4|AC1|AC2|AC3|FTP_20min|FTP_2x8|FTP_Ramp>
    name: "<kort, läsbart namn>"
    phase_appropriate: [prep, base_1, base_2, base_3, build_1, build_2, peak, race, recovery]

    parameterized: false  # true för AE-mallar och SS-mallar
    parameters:           # bara om parameterized: true
      duration_min:
        default: 60
        range: [30, 120]
        description: "..."

    intent: |
      Prosa-syfte. 2-5 meningar. Riktas till adepten.

    setting: outdoor|indoor|either
    terrain: flat|rolling|hilly|mixed   # bara om setting != indoor
    requires_trainer: false             # bara true om det är ett krav
    outdoor_only: false                 # bara true om det är ett krav
    erg_mode: false                     # workout-level default

    main_set:
      - segment: warmup|drills|main|recovery|cooldown|build|rest
        # välj EN av: duration_min, sets+duration_min, duration_pct, pattern
        # välj EN av: zone, effort_descriptor (pattern bär egna delsteg-zoner)
        cadence_rpm: [low, high]        # valfritt
        erg_mode: true|false            # override workout-level
        rest_sec: <vila efter segmentet om del av set>
        description: "valfri detalj"

    total_duration_min:
      estimated: 75
      flexible_range: [60, 90]
    zone_refs: [Z1, Z2, Z3]   # vilka zoner renderaren ska tabulera
    equipment: [power_meter, hr_strap]
    abort_conditions:
      - "..."
    coach_notes: |
      Internt — visas inte för adepten, men hjälper Nils/Trixa välja rätt pass.
```

## Skillnader mot sim- och löpbankerna

| Aspekt | Sim | Run | Bike |
|---|---|---|---|
| Primärt target | pace (CSS-offsets) | bpm + pace | **watt + bpm** |
| Distance vs duration | distance_m primärt | duration_min primärt | duration_min, alltid |
| Pace-reliabilitet | n/a | `pace_unreliable` på backar | n/a (watt alltid reliabelt) |
| Terräng-fält | nej | `terrain` + `surface` | `terrain` + `setting` |
| Indoor-möjlighet | endast pool | treadmill (sällan) | **trainer (centralt)** |
| Drill-segment | centralt | strides ersätter | **cadence-work ersätter** |
| ERG-mode | n/a | n/a | **central för trainer-pass** |

## Kategori-översikt

| Kategori | Typkoder | Innehållstyp |
|---|---|---|
| AE | AE1, AE2 | Parametriserade mallar (volym) |
| TE | TE1 | Konkreta varianter (3–5) |
| MF | MF1, MF2, MF3 | Konkreta (3–5 per typ) |
| SS | SS1, SS2 | Parametriserade mallar |
| ME | ME1, ME2, ME3, ME4 | Konkreta varianter (3–5 per typ) |
| AC | AC1, AC2, AC3 | Konkreta varianter (3–5 per typ) |
| T | FTP_20min, FTP_2x8, FTP_Ramp | Protokoll (en per variant) |

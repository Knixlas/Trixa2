# Löppassbank — schema

Samma grundschema som simbanken. Två running-specifika tillägg.

## Zon-system

Refererar `coach/engine/zones.py`. För löpning returnerar zones.py två
parallella zon-set från samma tröskel:

- `running_hr` — bpm-spann per zon, beräknat från AT_HR
- `running_pace` — sek/km-spann per zon, beräknat från threshold_pace_sec_per_km

Renderaren slår upp `zone: N` mot **båda** och formaterar t.ex.:

  > Z2: 142–151 bpm, 5:15–5:30 min/km

Om adepten saknar `threshold_pace_sec_per_km` (vanligt i prep innan löptest)
renderas bara bpm. Detta är OK — passet går att köra.

## Running-specifika fält

### `pace_unreliable: true`
Sätts på segment där pace inte är meningsfull som target:
- Backsegment (MF1, MF3, ME2, AC3, BME2)
- Mjuk terräng/teknisk stig
- Tävlingsmiljö med mycket vändningar

Renderaren utelämnar pace-spannet för dessa segment och skriver bara
HR-spann + segmentbeskrivning.

### `effort_descriptor`
Används istället för `zone` på segment där zonbegreppet inte appliceras väl:
- Strides (SS1) — för korta för att HR ska hinna ikapp
- Pickups med dynamisk progression (SS2)
- Race-pace-segment som inte mappar rent till en zon

Renderaren skriver ut descriptorn ordagrant utan att slå upp värden.

## Schema (sammanfattning)

```yaml
workouts:
  - code: <unik kod, t.ex. AE2_run_01>
    discipline: run
    category: <AE|TE|MF|SS|ME|AC|T>
    type_code: <AE1|AE2|TE1|MF1|MF2|MF3|SS1|SS2|ME1|ME2|ME3|ME4|AC1|AC2|AC3|RunTest>
    name: "<kort, läsbart namn>"
    phase_appropriate: [prep, base_1, base_2, base_3, build_1, build_2, peak, race]
    intent: |
      Prosa-syfte som renderaren skriver in i pass-presentationen.
      2-5 meningar. Riktas till adepten.
    main_set:
      - segment: warmup|drills|main|recovery|cooldown|build|rest
        # välj EN av: duration_min, distance_m, sets+(duration_min|distance_m)
        # välj EN av: zone, effort_descriptor
        rest_sec: <vila efter segmentet om del av set>
        pace_unreliable: false  # bara nödvändigt på hill-segment
        description: "valfri detalj"
    total_duration_min:
      estimated: 60
      flexible_range: [50, 75]
    zone_refs: [Z1, Z2, Z3]   # vilka zoner renderaren ska tabulera
    terrain: flat|rolling|hilly|track|mixed   # running-specifikt
    surface: road|trail|track|treadmill|any   # running-specifikt
    equipment: []
    abort_conditions:
      - "..."
    coach_notes: |
      Internt — visas inte för adepten, men hjälper Nils/Trixa välja rätt pass.
```

## Skillnader mot simbanken

| Aspekt | Sim | Run |
|---|---|---|
| Target-system | bara pace (CSS-offsets) | både bpm OCH pace |
| Drill-segment | centralt (catchup, six-kick m.fl.) | finns inte — strides ersätter |
| Hill-hantering | n/a | `pace_unreliable` flag |
| Terräng-fält | finns inte | `terrain`, `surface` |
| Distance vs duration | distance_m primärt | duration_min primärt, distance_m i intervaller |

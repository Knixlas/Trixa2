# Passbankens språk

Passtexten läses av adepten, ofta på telefonen, ofta av någon som aldrig
tränat strukturerat (docs/13). Den här guiden gäller allt adepten ser.
`coach/tests/test_passbank_sprak.py` stoppar orden i listan nedan.

## Vad adepten ser

| Fil | Fält som visas |
|---|---|
| pass (`*_AE.yaml` m.fl.) | `name`, `intent`, `main_set[].description`, `main_set[].effort_descriptor`, `main_set[].note`, `abort_conditions` |
| `swim_drills.yaml` | `name`, första raden av `intent` |
| `strength_exercises.yaml` | `name` |

`coach_notes` och övriga katalogfält (`execution`, `cues`,
`common_mistakes` …) är tränarens och visas inte för adepten. De skrivs inte om.

## Regler

1. **Skriv till "du".** Korta meningar. Inget "adepten".
2. **Svenska ord.** Engelska bara där det inte finns ett vedertaget svenskt.
3. **Förklara känslan, inte fysiologin.** "Pulsen kryper uppåt fast farten
   är densamma", inte "kardiovaskulär drift". Inga mitokondrier, kapillärer
   eller laktat.
4. **Zoner får stå kvar** (renderaren förklarar dem i Upplägg), men sätt
   gärna känslan först: "lugnt (Z2)".
5. **FTP och CSS bara inom parentes** efter en förklaring:
   "strax under tröskeln (88–94 % av FTP)". Den som saknar effektmätare
   ska kunna göra passet på känsla.
6. **Siffror som går att följa:** "4 × 400 m, vila 20 s" i stället för
   "4×400 @ CSS+4 r20".
7. **Ändra inte innehållet.** Samma pass, samma tider, samma säkerhetsråd.
   `{parameter}`-uttryck står kvar exakt.

## Ordlista

| Skriv inte | Skriv |
|---|---|
| HR | puls |
| bpm | slag/min |
| pace | tempo, fart (min/km) |
| strides | stegringar (korta fartökningar, 15–20 s) |
| pickups | fartökningar |
| drill, drills | teknikövning(ar) |
| aerob, aerobt | lugn uthållighet, lugnt |
| anaerob | så hårt att du snabbt blir andfådd |
| (kardiovaskulär) drift | pulsen kryper uppåt |
| RIR 2 | orka två till |
| RPE 7 | ansträngning 7 av 10 |
| ERG | låst effekt på trainern |
| IM, Ironman-race | Ironman |
| race, racet | loppet, tävlingen |
| peak | toppningen, toppformen |
| recovery | återhämtning |
| warmup, cooldown | uppvärmning, nedvarvning |
| kick | benspark |
| set, reps, rep | omgång(ar), repetitioner, gånger |
| kadens, rpm | trampfrekvens, varv/min |
| sighting | titta framåt (orientering) |
| VO2, VO2max | maxintervaller, syreupptagning |
| sweet spot | strax under tröskeln |
| negative split | andra halvan snabbare |
| split | mellantid |
| base-fasen, build | grundträningen, uppbyggnaden |
| T1, T2 | växlingen (sim → cykel, cykel → löp) |
| OW | öppet vatten |
| brick | kombinationspass (cykel och direkt löpning) |

"Tröskel", "intervaller" och "tempo" är svenska och får stå. Förklara
tröskeln första gången i ett pass: "tröskeln — den fart du orkar hålla i
ungefär en timme".

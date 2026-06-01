# Passbank — schema

Varje pass är en YAML-post. Filerna är grupperade per disciplin + kategori
(`swim\_AE.yaml`, `swim\_TE.yaml`, …). Drills lagras separat i
`swim\_drills.yaml` och refereras med kod från passen. Renderaren läser
pass + drills + adeptens zoner och producerar två outputs: människoläsbar
prosa och `.fit`-fil.

## Konventioner

* **Nycklar:** engelska, snake\_case
* **Värden/labels/notes:** svenska
* **Zoner som referenser**, aldrig hårdkodade paces. Renderaren slår upp `zone: 2` mot adeptens CSS och får ut t.ex. "1:38–1:40/100m".
* **Distans i meter** (inte yards). Pool conversion görs i renderaren om relevant.
* **Tid i sekunder** för delintervall (rest, send-off). Minuter för helpasstotaler.
* **Idempotens:** `code` är unik nyckel. Ändras aldrig efter att passet använts i en logg.

## Fält

|Fält|Typ|Krav|Beskrivning|
|-|-|-|-|
|`code`|str|✓|Unik. Format: `<TYPE>\_<DISCIPLINE>\_<NN>` t.ex. `AE2\_swim\_03`|
|`discipline`|enum|✓|`swim` / `bike` / `run` / `brick` / `strength`|
|`category`|enum|✓|`AE` / `TE` / `MF` / `SS` / `ME` / `AC` / `T` / `SP` / `BW`|
|`type\_code`|str|✓|Underkategori enligt 3.1, t.ex. `AE2`, `MF1`, `CSS\_Test`|
|`name`|str|✓|Mänskligt namn på svenska|
|`parameterized`|bool|–|True om passet är en mall med flex-parametrar (typiskt AE-volym)|
|`parameters`|obj|–|Bara om `parameterized: true`. Definierar duration-spann etc.|
|`phase\_appropriate`|list|✓|Vilka faser/perioder passet passar. Värden: `prep`, `base\_1`, `base\_2`, `base\_3`, `build\_1`, `build\_2`, `peak`, `race`, `recovery`|
|`intent`|str (multiline)|✓|Syftet med passet. Prosa. Renderaren citerar detta.|
|`main\_set`|list|✓|Strukturerad passinnehåll. Se nedan.|
|`total\_distance\_m`|int|–|Ungefärlig totaldistans (utelämnas vid parameterized)|
|`total\_duration\_min`|obj|✓|`estimated` + `flexible\_range: \[min, max]`|
|`zone\_refs`|list|✓|Vilka zoner passet rör sig i. T.ex. `\[Z1, Z2]`|
|`equipment`|list|✓|Tom lista om inget krävs. Värden: `paddles`, `pull\_buoy`, `fins`, `snorkel`, `kickboard`, `band`, `tempo\_trainer`|
|`abort\_conditions`|list|–|När adepten bör avbryta eller modifiera passet|
|`coach\_notes`|str (multiline)|–|Tränarkommentar — när det är lämpligt, vad man bör tänka på|

## `main\_set`-segment

Varje segment är ett objekt. Segmenttyper:

```yaml
- segment: warmup           # uppvärmning
  distance\_m: 400
  zone: 1
  description: "valfri stil, lugnt"

- segment: drills            # teknikövningar
  sets: 4
  distance\_m: 50
  rest\_sec: 15
  drills: \[catchup, six\_kick\_switch, fingertip\_drag]
  description: "rotera"

- segment: kick              # kickset
  sets: 4
  distance\_m: 50
  rest\_sec: 20
  equipment: \[kickboard]
  zone: 2

- segment: pull              # pull-set (buoy, ev. paddles)
  sets: 6
  distance\_m: 100
  rest\_sec: 15
  equipment: \[pull\_buoy, paddles]
  zone: 2

- segment: main              # huvudset
  sets: 4
  distance\_m: 400
  zone: 2
  rest\_sec: 20
  # ELLER send\_off\_sec: 420 för "leave on the 7:00"
  description: "håll jämn pace, andas på 3"

- segment: sprint            # snabbhetsdelar
  sets: 16
  distance\_m: 25
  rest\_sec: 30
  zone: 5
  description: "all-out från push, full återhämtning"

- segment: continuous        # för parametriserade mallar
  duration\_pct: 0.70         # procent av totalen
  zone: 2
  description: "stadig fart"

- segment: cooldown
  distance\_m: 200
  zone: 1
```

Sets kan också ha **descend/build/negative\_split**:

```yaml
- segment: main
  sets: 5
  distance\_m: 200
  rest\_sec: 20
  pace\_pattern: descending   # eller: ascending, alternating, build
  zones\_per\_set: \[2, 2, 3, 3, 4]   # alternativ till enskild `zone`
```

## Parameterized templates

För AE-volym där duration är poängen, inte ett specifikt protokoll:

```yaml
code: AE2\_swim\_template\_01
parameterized: true
parameters:
  duration\_min:
    min: 30
    max: 90
    default: 60
main\_set:
  - segment: warmup
    duration\_pct: 0.15
    zone: 1
  - segment: continuous
    duration\_pct: 0.70
    zone: 2
  - segment: cooldown
    duration\_pct: 0.15
    zone: 1
```

Renderaren räknar om procenten till minuter och uppskattar distans
mot adeptens CSS för att producera ett konkret pass.

## Drillkatalog (`swim\_drills.yaml`)

Drills är förstklassiga objekt — inte bara strängar. Varje drill har:

|Fält|Krav|Beskrivning|
|-|-|-|
|`code`|✓|Unik (snake\_case). Refereras från pass via `drills: \[code, …]`|
|`name`|✓|Mänskligt namn (på engelska/svenska beroende på etablerad term)|
|`category`|✓|`body\_position` / `rotation` / `catch\_pull` / `breathing` / `kick` / `speed\_tempo`|
|`difficulty`|✓|1-3 (grunddrill / tidigare erfarenhet / avancerad)|
|`intent`|✓|Vad drillen tränar och varför|
|`execution`|✓|Numrerad steg-för-steg-instruktion|
|`common\_mistakes`|✓|Lista — vanliga felaktigheter att kolla efter|
|`cues`|✓|Korta mentala bilder eller instruktioner|
|`equipment`|✓|Tom lista om inget krävs|
|`typical\_distance\_m`|✓|Rekommenderad distans per repetition (0 för stationära)|
|`related\_drills`|–|Lista av andra drill-koder som tränar liknande|
|`abort\_conditions`|–|För drills med säkerhetsdimension (undervattenarbete)|

### Drill-referenser i pass

I ett drill-segment refereras drills som lista av koder:

```yaml
- segment: drills
  sets: 4
  distance\_m: 50
  rest\_sec: 15
  drills: \[single\_arm, six\_kick\_switch, fingertip\_drag]
```

Verify-skriptet kontrollerar att alla referenser pekar på existerande
drills — stavfel fångas direkt.

Renderaren slår upp drill-namn och lägger optional drill-snabbreferens
sist i passet (en-rads-syfte per drill).

### Exkluderade drills

`catchup` ingår avsiktligt INTE i katalogen. Förklaring finns i
kommentaren överst i `swim\_drills.yaml`. Om en framtida adept har
en simcoach som föredrar den, lägg till den då — men dokumentera
beslutet.


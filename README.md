# Job-Scanner

Durchsucht öffentliche ATS-Job-Feeds vieler Firmen, filtert nach Schlagworten,
Region und Alter und schreibt die Treffer als `findings.md`. Dazu ein Helfer,
der zu einem Firmennamen automatisch das Job-Board (platform + slug) findet.

**Nur Python-Standardbibliothek** – kein `pip install` nötig. Auf macOS mit
`python3` aufrufen (nicht `python`).

Unterstützte Plattformen: **Personio** (XML), **Ashby**, **Lever**, **Workable**,
**Greenhouse**, **SmartRecruiters** (JSON).

---

## Schnellstart

```bash
cd job-scanner
python3 scan_jobs.py                 # Standard-Scan -> findings.md
python3 scan_jobs.py --near berlin   # nur Berlin + Remote
python3 find_slug.py "Firmenname"    # Board einer Firma finden + eintragen
```

---

## Dateien

| Datei | Zweck |
|---|---|
| `scan_jobs.py` | Scanner: Feeds abrufen, filtern, `findings.md` schreiben (rein lesend für die Configs). |
| `find_slug.py` | Findet `platform` + `slug` zu einem Firmennamen und trägt sie in `companies.json` ein. |
| `companies.json` | Firmenliste (`name` + `platform` + `slug`; Einträge dürfen **name-only** sein). NRW-Firmen oben → bestimmen die Reihenfolge in `findings.md`. |
| `buzzwords.json` | Filter-Schlagworte (Titel **und** Beschreibung). |
| `locations.json` | Benannte Regionen für `--near`. |
| `findings.md` | Ausgabe (wird bei jedem Lauf **überschrieben**, nicht angehängt). |

---

## `scan_jobs.py` – Parameter

| Parameter | Default | Bedeutung |
|---|---|---|
| *(keiner)* | – | Standard-Scan: Schlagwort-Filter + Geo-Filter + Altersfilter, Ausgabe `findings.md`. |
| `--all` | aus | **Kein** Titel/Beschreibungs-Filter – zeigt alle Stellen (Geo- und Altersfilter greifen weiter). |
| `--near [REGION]` | aus | Nur Stellen der Region (aus `locations.json`) **oder** Remote. Ohne Wert: `aktiv`-Region. Mit Wert: `--near berlin`, `--near hamburg`, … |
| `--worldwide` | aus | Geo-Filter **abschalten** (auch Jobs außerhalb Europas / nicht-remote im Ausland). |
| `--days N` | `60` | Nur Stellen der letzten **N Tage**. `--days 0` = ohne Altersfilter. |
| `--keyword KW` | – | Schlagwort setzen (mehrfach nutzbar); **überschreibt** `buzzwords.json` für diesen Lauf. |
| `--companies PFAD` | `companies.json` | Andere Firmenliste verwenden. |

### Was standardmäßig (ohne Flags) gefiltert wird
1. **Schlagworte** – Treffer, wenn ein Begriff aus `buzzwords.json` im Titel **oder** der Beschreibung steht (Teilstring, Groß-/Kleinschreibung egal). Mit `--all` aus.
2. **Geo** – nur **Europa** UND (**Deutschland** ODER **Remote**). Mit `--worldwide` aus.
3. **Alter** – nur Stellen ≤ `--days` Tage (Default 60). Stellen ohne Datum bleiben drin.

Reihenfolge in `findings.md` = Reihenfolge in `companies.json` (NRW zuerst), Stellen je Firma in Feed-Reihenfolge. Der Kopf der Datei zeigt die aktiven Filter.

### Beispiele
```bash
python3 scan_jobs.py                          # Standard (Data/Analytics, dbt; Europa+DE/Remote; 60 Tage)
python3 scan_jobs.py --near                    # zusätzlich nur Standard-Region (locations.json) oder Remote
python3 scan_jobs.py --near hamburg --days 14  # Hamburg/Remote, max. 14 Tage alt
python3 scan_jobs.py --all --days 0            # wirklich alles, ohne Titel-/Altersfilter
python3 scan_jobs.py --keyword dbt --keyword "data platform"   # eigene Schlagworte
python3 scan_jobs.py --worldwide               # auch Ausland/nicht-remote
```

---

## `find_slug.py` – Parameter

Findet zu einem Firmennamen das Board und trägt `platform` + `slug` in
`companies.json` ein (vorhandene `_`-Felder bleiben erhalten).

| Parameter | Default | Bedeutung |
|---|---|---|
| `NAME` (Positional) | – | Firmenname auflösen. **Ohne Angabe:** alle `name-only`-Einträge in `companies.json` auflösen. |
| `--yes`, `-y` | aus | Ohne Rückfrage eintragen (mit Kollisions-Bremse, s. u.). Nötig in nicht-interaktiven Umgebungen. |
| `--companies PFAD` | `companies.json` | Andere Firmenliste verwenden. |

### Beispiele
```bash
python3 find_slug.py "HERO Software"      # einen Namen auflösen (fragt j/n je Treffer)
python3 find_slug.py "FINN" --yes         # ohne Rückfrage eintragen
python3 find_slug.py                       # alle name-only-Einträge auflösen (interaktiv)
python3 find_slug.py --yes                 # alle name-only auflösen, ohne Rückfrage
```

> **Hinweis:** Ohne `--yes` fragt das Tool pro Treffer „j/n" – das braucht ein
> echtes Terminal. In Hintergrund-/Nicht-Terminal-Umgebungen `--yes` nutzen.

### Wie der Slug gefunden wird
1. **Raten** – Slug-Kandidaten aus dem Namen (inkl. untypischer Formen wie
   `firma.com`, `firma-`, `firmagmbh`), gegen alle 6 Feeds getestet.
2. **Websuche** (nur falls Raten leer) – best-effort; aus manchen Umgebungen
   geblockt → dann Meldung „Websuche blockiert", Slug manuell suchen.

Ein Slug wird nur übernommen, wenn der Feed echte Stellen liefert.

### Kollisions-Bremse
Jeder Treffer wird auf Fehlerquellen geprüft und ggf. ⚠ markiert:
- **generischer Kurz-Slug** (`pure` für „Pure Energy"),
- **Slug ohne Namensbezug** (`nice` für „Cognigy" nach Übernahme),
- **alle Stellen außerhalb Europas** (fremde Firma).

Im `--yes`-Modus werden verdächtige Treffer **nicht** automatisch eingetragen;
interaktiv siehst du die Warnung und entscheidest selbst.

---

## Config-Dateien pflegen

### `companies.json` – Firmen
Neue Firma = ein Eintrag. `platform` ∈ `personio | ashby | lever | workable |
greenhouse | smartrecruiters`. Du kannst auch **nur den Namen** eintragen und
`find_slug.py` den Rest ergänzen lassen:

```json
{ "name": "Beispiel GmbH" }
{ "name": "Beispiel GmbH", "platform": "personio", "slug": "beispiel", "_ort": "Köln" }
```
Felder mit `_`-Prefix (`_ort`, `_quelle`, `_hinweis`) werden ignoriert.

**Woher der `slug`?** Aus der öffentlichen Stellen-URL:

| Plattform | URL-Muster | Beispiel-Slug |
|---|---|---|
| personio | `https://SLUG.jobs.personio.de/...` | `adsquare` |
| ashby | `https://jobs.ashbyhq.com/SLUG/...` | `enpal` |
| lever | `https://jobs.lever.co/SLUG/...` | `finn` |
| workable | `https://apply.workable.com/SLUG/...` | `hero-software` |
| greenhouse | `https://boards.greenhouse.io/SLUG/...` | `stripe` |
| smartrecruiters | `https://jobs.smartrecruiters.com/SLUG/...` | `redcare-pharmacy` |

### `buzzwords.json` – Filter-Begriffe
```json
{ "buzzwords": ["dbt", "analytics engineer", "data engineer", "..."] }
```
Teilstring-Match (kein Wortgrenzen-Check): `sql` matcht auch „PostgreSQL".
Per CLI temporär überschreiben: `--keyword …`.

### `locations.json` – Regionen für `--near`
```json
{
  "aktiv": "nrw",
  "regionen": {
    "nrw": ["köln", "aachen", "bonn", "düsseldorf", "…"],
    "hamburg": ["hamburg"],
    "berlin": ["berlin"]
  }
}
```
`--near` ohne Wert nutzt `aktiv`; `--near hamburg` wählt explizit. Remote-Stellen
zählen in **jeder** Region. Unbekannte Region → Warnung + Liste + Fallback.

---

## Typischer Ablauf
```bash
# 1) Firmen (auch nur mit Namen) in companies.json eintragen
# 2) Slugs auflösen lassen
python3 find_slug.py --yes
# 3) Scannen
python3 scan_jobs.py --near --days 30
# 4) Treffer in findings.md ansehen
```

---

## Optionale Pakete & Stolpersteine
- **`certifi`** – CA-Zertifikate (macOS-Python kennt System-Zertifikate oft
  nicht). Bei `CERTIFICATE_VERIFY_FAILED`: `python3 -m pip install certifi`.
- **`defusedxml`** – härteres XML-Parsing (optional; sonst Stdlib-Fallback, der
  XML mit DTD/ENTITY ablehnt).
- **HTTP 429 (Too Many Requests)** – v. a. Workable bei vielen Läufen kurz
  hintereinander. Transient; betroffene Firmen kommen beim nächsten Lauf wieder.
- **„Websuche blockiert"** in `find_slug` – Suchmaschinen blocken automatisierte
  Abrufe; Slug dann manuell aus der Stellen-URL eintragen.
- **`command not found: python`** – auf macOS `python3` verwenden.

#!/usr/bin/env python3
"""
Job-Scanner für ATS-Plattformen mit öffentlichem Feed.

Fragt für jede Firma in companies.json den öffentlichen Job-Feed ab
(Personio XML, Ashby/Lever/Workable JSON), normalisiert die Stellen auf
ein gemeinsames Format und filtert nach Titel-Keywords. Ergebnis als
Konsolen-Tabelle + Markdown-Datei (findings.md).

Keine externen Abhängigkeiten – nur Python-Standardbibliothek.

Beispiele:
  python3 scan_jobs.py                      # Standard-Filter (Data/Analytics Engineer, dbt)
  python3 scan_jobs.py --all                # alle Stellen, kein Titel-Filter
  python3 scan_jobs.py --keyword dbt --keyword "data engineer"
  python3 scan_jobs.py --near               # nur NRW-Städte oder Remote
"""

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPANIES_FILE = HERE / "companies.json"
BUZZWORDS_FILE = HERE / "buzzwords.json"
LOCATIONS_FILE = HERE / "locations.json"
OUTPUT_MD = HERE / "findings.md"

# Realistischer Browser-UA: manche Feeds (Ashby/Workable) sitzen hinter
# Cloudflare und blocken untypische User-Agents mit 403.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 20

# ── Konfiguration: hier anpassen ────────────────────────────────────────────
# Fallback-Schlagworte, falls buzzwords.json fehlt. Die eigentliche
# Pflege der Liste passiert in buzzwords.json (Titel + Beschreibung).
DEFAULT_KEYWORDS = [
    "dbt",
    "analytics engineer",
    "data engineer",
    "data platform",
    "data warehouse",
]

# Für --near: Stelle gilt als regional passend, wenn Ort einen dieser
# Begriffe enthält ODER die Stelle als Remote markiert ist.
REGIO_BEGRIFFE = [
    # NRW-Städte
    "köln", "cologne", "bonn", "düsseldorf", "duesseldorf", "aachen",
    "rheinbach", "dortmund", "essen", "bochum", "münster", "muenster",
    "duisburg", "wuppertal", "mönchengladbach", "leverkusen", "krefeld",
    "nrw", "nordrhein", "north rhine",
    # deutschlandweit erreichbar (von Rheinbach aus pendel-/remote-fähig)
    "germany", "deutschland", "dach",
]

# Geo-Filter (Standard): nur Europa + (Deutschland ODER Remote). Mit --worldwide aus.
# Vollnamen/Städte werden als Substring geprüft, kurze Länder-Codes nur als Token
# (sonst würde z.B. "Campus" als "us" gelten).
_DE_WORTE = [
    "germany", "deutschland", "berlin", "münchen", "munchen", "munich", "hamburg",
    "köln", "cologne", "frankfurt", "stuttgart", "düsseldorf", "duesseldorf",
    "dortmund", "essen", "bonn", "aachen", "mannheim", "karlsruhe", "leipzig",
    "dresden", "nürnberg", "nuremberg", "hannover", "hanover", "bremen", "münster",
    "muenster", "mönchengladbach", "mülheim", "moers", "offenbach", "koblenz",
    "heidelberg", "darmstadt", "rheinbach", "fellbach", "bochum", "duisburg",
    "wuppertal", "leverkusen", "krefeld", "wiesbaden", "augsburg",
]
_NICHTEU_WORTE = [
    "united states", "usa", "america", "new york", "san francisco", "boston",
    "austin", "chicago", "seattle", "los angeles", "california", "texas", "denver",
    "atlanta", "miami", "canada", "toronto", "vancouver", "montreal", "india",
    "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune", "singapore",
    "dubai", "abu dhabi", "qatar", "australia", "sydney", "melbourne", "japan",
    "tokyo", "shanghai", "hong kong", "brazil", "são paulo", "sao paulo", "mexico",
    "argentina", "manila", "bangkok", "jakarta", "tel aviv", "seoul", "south africa",
    "egypt", "kenya", "nigeria", "bogota", "bogotá", "kuala lumpur", "taiwan",
]
_EUROPA_WORTE = [
    "europe", "european", "emea", "austria", "österreich", "vienna", "wien", "linz",
    "graz", "switzerland", "schweiz", "zurich", "zürich", "geneva", "basel", "bern",
    "lausanne", "gallen", "united kingdom", "london", "england", "manchester",
    "ireland", "dublin", "france", "paris", "lyon", "spain", "madrid", "barcelona",
    "valencia", "italy", "milan", "rome", "netherlands", "amsterdam", "rotterdam",
    "belgium", "brussels", "antwerp", "poland", "warsaw", "krakow", "kraków", "sweden",
    "stockholm", "denmark", "copenhagen", "norway", "oslo", "finland", "helsinki",
    "portugal", "lisbon", "porto", "prague", "bucharest", "budapest", "athens",
    "sofia", "belgrade", "tallinn", "vilnius", "riga", "zagreb", "ljubljana",
    "bratislava", "luxembourg", "cyprus", "malta",
]


# ── HTTP-Helfer ──────────────────────────────────────────────────────────────
def _ssl_context():
    """SSL-Kontext mit certifi-CA-Bundle, falls vorhanden (macOS-Python kennt
    die System-Zertifikate oft nicht). Sonst Standard-Kontext."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/xml, */*",
        "Accept-Language": "de,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
        return resp.read()


def _get_json(url):
    return json.loads(_get(url).decode("utf-8"))


def _parse_xml_safe(data):
    """XML aus fremder Quelle sicher parsen (gegen XXE / billion-laughs).

    Nutzt defusedxml, falls installiert. Sonst Stdlib-Parser, bei dem
    Entity-Deklarationen blockiert werden – das verhindert Entity-Expansion.
    """
    try:
        import defusedxml.ElementTree as DET  # optional, falls vorhanden
        return DET.fromstring(data)
    except ImportError:
        pass
    # Stdlib-Fallback: Entity-Expansion-Angriffe (billion-laughs / XXE) brauchen
    # eine DTD- oder ENTITY-Deklaration. Wir lehnen solche Dokumente komplett ab.
    head = data[:4096].lower() if isinstance(data, (bytes, bytearray)) else data[:4096].encode().lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ValueError("XML mit DTD/ENTITY abgelehnt (Sicherheits-Schutz)")
    return ET.fromstring(data)


def _epoch_to_date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def _iso_to_date(s):
    if not s:
        return ""
    return str(s)[:10]


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(*parts):
    """Mehrere (ggf. HTML-)Textteile zu reinem Kleinbuchstaben-Text verbinden.
    Dient nur dem Keyword-Matching, nicht der Anzeige."""
    text = " ".join(str(p) for p in parts if p)
    return _TAG_RE.sub(" ", text).lower()


# ── Plattform-Fetcher: liefern je eine Liste normalisierter Stellen ──────────
# Normalisiertes Format:
#   {firma, titel, ort, remote(bool), link, datum(str), plattform}

def fetch_personio(name, slug):
    last_err = None
    for tld in ("de", "com"):
        url = f"https://{slug}.jobs.personio.{tld}/xml?language=en"
        try:
            root = _parse_xml_safe(_get(url))
        except (urllib.error.HTTPError, urllib.error.URLError, ET.ParseError) as e:
            last_err = e
            continue
        jobs = []
        for pos in root.iter("position"):
            def t(tag):
                el = pos.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            jid = t("id")
            office = t("office")
            desc_el = pos.find("jobDescriptions")
            beschreibung = _strip_html(
                t("department"), t("recruitingCategory"),
                "".join(desc_el.itertext()) if desc_el is not None else "",
            )
            jobs.append({
                "firma": name,
                "titel": t("name"),
                "ort": office,
                "remote": "remote" in office.lower(),
                "link": f"https://{slug}.jobs.personio.{tld}/job/{jid}?language=en" if jid else url,
                "datum": _iso_to_date(t("createdAt")),
                "plattform": "personio",
                "beschreibung": beschreibung,
            })
        return jobs
    raise last_err if last_err else RuntimeError("personio: unbekannter Fehler")


def fetch_ashby(name, slug):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "firma": name,
            "titel": j.get("title", ""),
            "ort": j.get("location", ""),
            "remote": bool(j.get("isRemote")),
            "link": j.get("jobUrl") or j.get("applyUrl", ""),
            "datum": _iso_to_date(j.get("publishedAt")),
            "plattform": "ashby",
            "beschreibung": _strip_html(j.get("descriptionPlain") or j.get("descriptionHtml")),
        })
    return jobs


def fetch_lever(name, slug):
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        wp = (j.get("workplaceType") or "").lower()
        # Volltext: Intro + strukturierte Listen (Anforderungen etc.) + Abschluss
        listen = " ".join(
            (lst.get("text", "") + " " + lst.get("content", ""))
            for lst in (j.get("lists") or [])
        )
        beschreibung = _strip_html(
            j.get("descriptionPlain") or j.get("description"),
            listen,
            j.get("additionalPlain") or j.get("additional"),
        )
        jobs.append({
            "firma": name,
            "titel": j.get("text", ""),
            "ort": cats.get("location", ""),
            "remote": wp == "remote",
            "link": j.get("hostedUrl", ""),
            "datum": _epoch_to_date(j.get("createdAt")),
            "plattform": "lever",
            "beschreibung": beschreibung,
        })
    return jobs


def fetch_workable(name, slug):
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    # Workable liefert für stillgelegte/aliasierte Accounts generisch den eigenen
    # "Workable"-Account zurück (statt 404) – das ist kein echter Firmen-Treffer.
    if (data.get("name") or "").strip().lower() == "workable":
        return []
    jobs = []
    for j in data.get("jobs", []):
        ort = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        jobs.append({
            "firma": name,
            "titel": j.get("title", ""),
            "ort": ort,
            "remote": bool(j.get("remote")) or "remote" in ort.lower(),
            "link": j.get("url") or j.get("application_url") or j.get("shortlink", ""),
            "datum": _iso_to_date(j.get("published_on") or j.get("created_at")),
            "plattform": "workable",
            "beschreibung": _strip_html(
                j.get("description"), j.get("requirements"), j.get("benefits")
            ),
        })
    return jobs


def fetch_greenhouse(name, slug):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    jobs = []
    for j in data.get("jobs", []):
        ort = ((j.get("location") or {}).get("name")) or ""
        jobs.append({
            "firma": name,
            "titel": j.get("title", ""),
            "ort": ort,
            "remote": "remote" in ort.lower(),
            "link": j.get("absolute_url", ""),
            "datum": _iso_to_date(j.get("updated_at")),
            "plattform": "greenhouse",
            "beschreibung": _strip_html(j.get("content")),
        })
    return jobs


def _sr_label(v):
    """SmartRecruiters-Felder sind teils {id,label}-Objekte, teils Strings."""
    return v.get("label", "") if isinstance(v, dict) else (v or "")


def fetch_smartrecruiters(name, slug):
    # Listen-Endpunkt liefert max. 100 pro Seite → paginieren (Deckel gegen Runaway).
    postings = []
    offset = 0
    while offset < 2000:
        data = _get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}"
        )
        batch = data.get("content", [])
        postings.extend(batch)
        if len(batch) < 100 or len(postings) >= data.get("totalFound", 0):
            break
        offset += 100

    jobs = []
    for j in postings:
        loc = j.get("location") or {}
        ort = ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
        jid = j.get("id", "")
        # Listen-Endpunkt liefert keinen Beschreibungstext – nur Metadaten matchen.
        beschreibung = _strip_html(
            _sr_label(j.get("department")), _sr_label(j.get("function")),
            _sr_label(j.get("industry")),
        )
        jobs.append({
            "firma": name,
            "titel": j.get("name", ""),
            "ort": ort,
            "remote": bool(loc.get("remote")) or "remote" in ort.lower(),
            "link": f"https://jobs.smartrecruiters.com/{slug}/{jid}" if jid else "",
            "datum": _iso_to_date(j.get("releasedDate")),
            "plattform": "smartrecruiters",
            "beschreibung": beschreibung,
        })
    return jobs


FETCHERS = {
    "personio": fetch_personio,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "workable": fetch_workable,
    "greenhouse": fetch_greenhouse,
    "smartrecruiters": fetch_smartrecruiters,
}


# ── Filter ───────────────────────────────────────────────────────────────────
def load_keywords(args):
    """Schlagworte aus CLI (--keyword), sonst buzzwords.json, sonst Default."""
    if args.keyword:
        return args.keyword
    try:
        data = json.loads(BUZZWORDS_FILE.read_text(encoding="utf-8"))
        worte = [w for w in data.get("buzzwords", []) if w]
        if worte:
            return worte
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return DEFAULT_KEYWORDS


def matched_keywords(job, keywords):
    """Liste der Schlagworte, die in Titel ODER Beschreibung vorkommen."""
    haystack = (job["titel"] + " " + job.get("beschreibung", "")).lower()
    return [kw for kw in keywords if kw.lower() in haystack]


def load_regionen():
    """Regionen aus locations.json laden → (regionen_dict, default_name).
    Fallback auf NRW (REGIO_BEGRIFFE), falls die Datei fehlt/kaputt ist."""
    try:
        data = json.loads(LOCATIONS_FILE.read_text(encoding="utf-8"))
        regionen = {k.lower(): v for k, v in data.get("regionen", {}).items() if v}
        default = (data.get("aktiv") or "nrw").lower()
        if regionen:
            return regionen, default
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"nrw": REGIO_BEGRIFFE}, "nrw"


def matches_regio(job, terms):
    """True, wenn die Stelle remote ist ODER ihr Ort einen Region-Begriff enthält."""
    if job["remote"]:
        return True
    ort = job["ort"].lower()
    return any(t.lower() in ort for t in terms)


def _standort_klasse(job):
    """Grobklassifizierung des Job-Standorts: 'de' | 'eu' | 'noneu' | '?'."""
    ort = job["ort"].lower()
    tokens = set(re.findall(r"[a-zäöüß]+", ort))
    def hat(worte, codes):
        return any(w in ort for w in worte) or bool(tokens & codes)
    if hat(_DE_WORTE, {"de", "deu", "ger"}):
        return "de"
    if hat(_NICHTEU_WORTE, {"us", "usa", "uae"}):
        return "noneu"
    if hat(_EUROPA_WORTE, {"eu", "uk"}):
        return "eu"
    return "?"


def ist_aktuell(job, max_age_tage, heute):
    """True, wenn die Stelle ≤ max_age_tage alt ist. Stellen ohne (parsebares)
    Datum werden behalten (nicht verstecken, da Alter unbekannt)."""
    d = (job.get("datum") or "")[:10]
    if not d:
        return True
    try:
        tag = date.fromisoformat(d)
    except ValueError:
        return True
    return (heute - tag).days <= max_age_tage


def geo_ok(job):
    """Standard-Geo-Filter: in DE → ja; außerhalb Europas → nein;
    Europa (nicht DE) oder unbekannter Ort → nur wenn remote."""
    klasse = _standort_klasse(job)
    if klasse == "de":
        return True
    if klasse == "noneu":
        return False
    return job["remote"]


# ── Ausgabe ──────────────────────────────────────────────────────────────────
def print_table(jobs):
    if not jobs:
        print("Keine Treffer.")
        return
    cols = [
        ("firma", "Firma", 16),
        ("titel", "Titel", 44),
        ("ort", "Ort", 20),
    ]
    # Link als letzte Spalte ohne Padding → bleibt im Terminal klickbar.
    header = "  ".join(h.ljust(w) for _, h, w in cols) + "  Rem  Link"
    print(header)
    print("-" * (len(header) + 30))
    for j in jobs:
        line = "  ".join(str(j[k])[:w].ljust(w) for k, _, w in cols)
        line += "  " + ("✓  " if j["remote"] else "   ") + j["link"]
        print(line)


def write_markdown(jobs, args):
    lines = [
        "# Job-Scanner – Treffer",
        "",
        f"*Stand: siehe Dateidatum · Filter: "
        + ("**alle Stellen**" if args.all else "Schlagworte in Titel + Beschreibung")
        + ("" if args.worldwide else " · Europa + (DE oder Remote)")
        + (f" · ≤ {args.days} Tage" if args.days > 0 else "")
        + (f" · Region: {args.aktive_region} + Remote" if getattr(args, "aktive_region", None) else "")
        + "*",
        "",
        "| Firma | Titel | Ort | Remote | Schlagworte | Plattform | Datum | Link |",
        "|---|---|---|:---:|---|---|---|---|",
    ]
    for j in jobs:
        rem = "✓" if j["remote"] else ""
        link = f"[öffnen]({j['link']})" if j["link"] else ""
        titel = j["titel"].replace("|", "\\|")
        schlagworte = ", ".join(j.get("treffer_worte", []))
        lines.append(
            f"| {j['firma']} | {titel} | {j['ort']} | {rem} | {schlagworte} | "
            f"{j['plattform']} | {j['datum']} | {link} |"
        )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Scannt ATS-Feeds nach passenden Stellen.")
    ap.add_argument("--all", action="store_true", help="kein Titel-Filter, alle Stellen zeigen")
    ap.add_argument("--near", nargs="?", const="", default=None, metavar="REGION",
                    help="nur Stellen einer Region (aus locations.json) oder Remote. "
                         "Ohne Wert: Standard-Region; z.B. --near berlin")
    ap.add_argument("--worldwide", action="store_true",
                    help="Geo-Filter aus (auch Jobs außerhalb Europas / nicht-remote im Ausland)")
    ap.add_argument("--days", type=int, default=60,
                    help="nur Stellen der letzten N Tage (Standard 60; 0 = ohne Altersfilter)")
    ap.add_argument("--keyword", action="append", default=[],
                    help="Schlagwort (mehrfach nutzbar); überschreibt buzzwords.json")
    ap.add_argument("--companies", default=str(COMPANIES_FILE), help="Pfad zur companies.json")
    args = ap.parse_args()

    keywords = load_keywords(args)

    firmen = json.loads(Path(args.companies).read_text(encoding="utf-8")).get("firmen", [])

    alle_jobs = []
    print(f"Scanne {len(firmen)} Firmen …\n", file=sys.stderr)
    for f in firmen:
        name, platform, slug = f.get("name"), f.get("platform"), f.get("slug")
        if not platform or not slug:
            print(f'  ⚠ {name}: kein Slug – auflösen mit  python3 find_slug.py "{name}"',
                  file=sys.stderr)
            continue
        fetcher = FETCHERS.get(platform)
        if not fetcher:
            print(f"  ⚠ {name}: unbekannte Plattform '{platform}'", file=sys.stderr)
            continue
        try:
            jobs = fetcher(name, slug)
            alle_jobs.extend(jobs)
            print(f"  ✓ {name:18} {len(jobs):>3} Stellen ({platform})", file=sys.stderr)
        except Exception as e:  # bewusst breit: eine Firma soll den Lauf nicht stoppen
            print(f"  ✗ {name:18} Fehler: {e} ({platform}/{slug})", file=sys.stderr)

    # Treffer-Schlagworte erfassen; ohne Treffer wird (außer bei --all) gefiltert.
    for j in alle_jobs:
        j["treffer_worte"] = matched_keywords(j, keywords)

    treffer = alle_jobs if args.all else [j for j in alle_jobs if j["treffer_worte"]]
    # Altersfilter: nur Stellen der letzten N Tage (Standard 60; --days 0 = aus).
    if args.days > 0:
        heute = date.today()
        treffer = [j for j in treffer if ist_aktuell(j, args.days, heute)]
    # Standard-Geo-Filter: Europa + (Deutschland oder Remote); mit --worldwide aus.
    if not args.worldwide:
        treffer = [j for j in treffer if geo_ok(j)]
    args.aktive_region = None
    if args.near is not None:
        regionen, default_region = load_regionen()
        region = (args.near or default_region).lower()
        terms = regionen.get(region)
        if terms is None:
            print(f"  ⚠ Region '{region}' unbekannt – verfügbar: {', '.join(sorted(regionen))}. "
                  f"Nutze '{default_region}'.", file=sys.stderr)
            region, terms = default_region, regionen.get(default_region, [])
        args.aktive_region = region
        treffer = [j for j in treffer if matches_regio(j, terms)]

    # Reihenfolge = Scan-Lauf (companies.json-Reihenfolge × Stellen je Feed),
    # bewusst NICHT alphabetisch sortiert.

    print(f"\n{len(treffer)} Treffer von {len(alle_jobs)} Stellen gesamt:\n", file=sys.stderr)
    print_table(treffer)
    write_markdown(treffer, args)
    print(f"\n→ Markdown geschrieben: {OUTPUT_MD}", file=sys.stderr)


if __name__ == "__main__":
    main()

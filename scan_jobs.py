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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPANIES_FILE = HERE / "companies.json"
BUZZWORDS_FILE = HERE / "buzzwords.json"
LOCATIONS_FILE = HERE / "locations.json"
OUTPUT_MD = HERE / "findings.md"
SEEN_FILE = HERE / "seen.json"  # Zustand: schon einmal gesehene Stellen (für 🆕-Markierung)

# Anzahl paralleler Feed-Abrufe. Stdlib-Threads reichen, da die Arbeit I/O-gebunden
# ist (Warten auf HTTP). Moderat halten, um keine 429er zu provozieren.
MAX_WORKERS = 8

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

# Negativ-Schlagworte: matcht eines davon im TITEL, wird die Stelle verworfen –
# auch wenn ein Buzzword passt. Bewusst nur Titel (nicht Beschreibung), damit z.B.
# "kein Praktikum"-Erwähnungen im Fließtext nicht fälschlich aussortieren.
# Pflege in buzzwords.json unter "exclude"; hier nur der Fallback.
DEFAULT_EXCLUDE = [
    "werkstudent", "working student", "praktikum", "praktikant", "intern ",
    "internship", "ausbildung", "azubi", "trainee", "vertrieb", "sales",
    "(junior)", "duales studium", "dual student",
]

# Discovery via Bundesagentur für Arbeit (öffentliche Jobsuche-API). Anders als die
# ATS-Feeds ist das eine Volltext-Suche über ALLE gemeldeten Stellen – findet damit
# auch Firmen, die (noch) nicht in companies.json stehen. Der API-Key ist der
# öffentlich dokumentierte Schlüssel der Jobsuche.
ARBEITSAGENTUR_API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
ARBEITSAGENTUR_KEY = "jobboerse-jobsuche"
# Default-Suchbegriffe für --discover. Die API-Suche ist unscharf (liefert auch
# Unpassendes); die Treffer laufen anschließend durch den normalen Buzzword-Filter.
DEFAULT_DISCOVER_QUERIES = ["analytics engineer", "data engineer", "data platform", "dbt"]

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


def fetch_recruitee(name, slug):
    data = _get_json(f"https://{slug}.recruitee.com/api/offers/")
    jobs = []
    for j in data.get("offers", []):
        ort = j.get("location") or ", ".join(
            x for x in (j.get("city"), j.get("country")) if x
        )
        jobs.append({
            "firma": name,
            "titel": j.get("title", ""),
            "ort": ort,
            "remote": bool(j.get("remote")) or bool(j.get("hybrid")) or "remote" in ort.lower(),
            "link": j.get("careers_url") or j.get("careers_apply_url", ""),
            "datum": _iso_to_date(j.get("published_at") or j.get("created_at")),
            "plattform": "recruitee",
            "beschreibung": _strip_html(
                j.get("description"), j.get("requirements"), _sr_label(j.get("department")),
            ),
        })
    return jobs


FETCHERS = {
    "personio": fetch_personio,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "workable": fetch_workable,
    "greenhouse": fetch_greenhouse,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
}


# ── Discovery-Quelle (query-basiert, nicht firmenzentrisch) ──────────────────
def fetch_arbeitsagentur(query, ort, umkreis, max_seiten=5):
    """Bundesagentur-Jobsuche nach einem Suchbegriff im Umkreis eines Orts.

    Liefert normalisierte Stellen (ohne Beschreibungstext – die Liste enthält nur
    Metadaten, daher Matching nur über den Titel, analog SmartRecruiters)."""
    jobs = []
    seen_refs = set()
    for seite in range(1, max_seiten + 1):
        params = urllib.parse.urlencode({
            "was": query, "wo": ort, "umkreis": umkreis, "size": 100, "page": seite,
        })
        req = urllib.request.Request(
            f"{ARBEITSAGENTUR_API}?{params}",
            headers={"User-Agent": USER_AGENT, "X-API-Key": ARBEITSAGENTUR_KEY,
                     "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        angebote = data.get("stellenangebote", []) or []
        if not angebote:
            break
        for s in angebote:
            ref = s.get("refnr", "")
            if not ref or ref in seen_refs:
                continue
            seen_refs.add(ref)
            ao = s.get("arbeitsort") or {}
            ort_str = ", ".join(x for x in (ao.get("ort"), ao.get("region")) if x)
            jobs.append({
                "firma": s.get("arbeitgeber", "") or "?",
                "titel": s.get("titel") or s.get("beruf", ""),
                "ort": ort_str,
                "remote": False,  # API kennzeichnet Remote nicht zuverlässig
                "link": "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
                        + urllib.parse.quote(ref, safe=""),
                "datum": _iso_to_date(s.get("aktuelleVeroeffentlichungsdatum")),
                "plattform": "arbeitsagentur",
                "beschreibung": "",  # Liste liefert keinen Volltext
            })
        if len(angebote) < 100:
            break
    return jobs


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


def load_excludes(args):
    """Negativ-Schlagworte aus CLI (--exclude), sonst buzzwords.json ('exclude'),
    sonst Default. Leere Liste (--no-exclude) schaltet den Filter ab."""
    if getattr(args, "no_exclude", False):
        return []
    if args.exclude:
        return args.exclude
    try:
        data = json.loads(BUZZWORDS_FILE.read_text(encoding="utf-8"))
        worte = [w for w in data.get("exclude", []) if w]
        if worte:
            return worte
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return DEFAULT_EXCLUDE


def is_excluded(job, excludes):
    """True, wenn ein Negativ-Schlagwort im TITEL vorkommt (Beschreibung bewusst nicht)."""
    titel = job["titel"].lower()
    return any(x.lower() in titel for x in excludes)


# ── Zustand: schon gesehene Stellen (für 🆕 „neu seit letztem Lauf") ──────────
def job_key(job):
    """Stabiler Schlüssel je Stelle. Link bevorzugt (eindeutig); sonst Fallback
    auf Firma|Titel|Ort, damit linklose Treffer trotzdem wiedererkannt werden."""
    return (job.get("link") or f"{job['firma']}|{job['titel']}|{job['ort']}").strip().lower()


def load_seen():
    """Gesehene Stellen laden → {key: erstes_sichtungsdatum}."""
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")


def dedupe(jobs):
    """Stellen über alle Quellen hinweg per job_key entfalten (erste gewinnt).
    Discovery (Arbeitsagentur) und ATS-Feed können dieselbe Stelle liefern."""
    gesehen, raus = set(), []
    for j in jobs:
        k = job_key(j)
        if k in gesehen:
            continue
        gesehen.add(k)
        raus.append(j)
    return raus


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
    header = "Neu  " + "  ".join(h.ljust(w) for _, h, w in cols) + "  Rem  Link"
    print(header)
    print("-" * (len(header) + 30))
    for j in jobs:
        line = ("🆕  " if j.get("neu") else "    ")
        line += "  ".join(str(j[k])[:w].ljust(w) for k, _, w in cols)
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
        "| Neu | Firma | Titel | Ort | Remote | Schlagworte | Plattform | Datum | Link |",
        "|:---:|---|---|---|:---:|---|---|---|---|",
    ]
    for j in jobs:
        neu = "🆕" if j.get("neu") else ""
        rem = "✓" if j["remote"] else ""
        link = f"[öffnen]({j['link']})" if j["link"] else ""
        titel = j["titel"].replace("|", "\\|")
        schlagworte = ", ".join(j.get("treffer_worte", []))
        lines.append(
            f"| {neu} | {j['firma']} | {titel} | {j['ort']} | {rem} | {schlagworte} | "
            f"{j['plattform']} | {j['datum']} | {link} |"
        )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Feed-Abruf (parallel) ─────────────────────────────────────────────────────
def _scan_company(f):
    """Eine Firma abrufen. Gibt (status, name, info, jobs) zurück; wirft nie –
    eine kaputte Firma darf den (parallelen) Lauf nicht stoppen."""
    name, platform, slug = f.get("name"), f.get("platform"), f.get("slug")
    if not platform or not slug:
        return ("skip", name, f'kein Slug – auflösen mit  python3 find_slug.py "{name}"', [])
    fetcher = FETCHERS.get(platform)
    if not fetcher:
        return ("skip", name, f"unbekannte Plattform '{platform}'", [])
    try:
        jobs = fetcher(name, slug)
        return ("ok", name, f"{len(jobs):>3} Stellen ({platform})", jobs)
    except Exception as e:  # bewusst breit
        return ("err", name, f"Fehler: {e} ({platform}/{slug})", [])


def scan_feeds(firmen):
    """Alle Firmen-Feeds parallel abrufen (I/O-gebunden → Threads)."""
    alle_jobs = []
    print(f"Scanne {len(firmen)} Firmen (parallel, {MAX_WORKERS} Worker) …\n", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for status, name, info, jobs in pool.map(_scan_company, firmen):
            marker = {"ok": "✓", "skip": "⚠", "err": "✗"}[status]
            print(f"  {marker} {str(name):18} {info}", file=sys.stderr)
            alle_jobs.extend(jobs)
    return alle_jobs


def discover_arbeitsagentur(queries, ort, umkreis):
    """Discovery-Suche über die Bundesagentur, je Suchbegriff parallel."""
    alle_jobs = []
    print(f"\nDiscovery (Arbeitsagentur): {len(queries)} Suchbegriffe um '{ort}' "
          f"(+{umkreis} km) …", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(queries) or 1)) as pool:
        def one(q):
            try:
                return q, fetch_arbeitsagentur(q, ort, umkreis), None
            except Exception as e:
                return q, [], e
        for q, jobs, err in pool.map(one, queries):
            if err:
                print(f"  ✗ '{q}': {err}", file=sys.stderr)
            else:
                print(f"  ✓ '{q}': {len(jobs):>3} Stellen", file=sys.stderr)
                alle_jobs.extend(jobs)
    return alle_jobs


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
    ap.add_argument("--exclude", action="append", default=[],
                    help="Negativ-Schlagwort im Titel (mehrfach); überschreibt buzzwords.json")
    ap.add_argument("--no-exclude", action="store_true", help="Negativ-Filter abschalten")
    ap.add_argument("--discover", action="store_true",
                    help="zusätzlich die Bundesagentur-Jobsuche anzapfen (findet auch "
                         "Firmen außerhalb companies.json)")
    ap.add_argument("--discover-ort", default="Bonn", metavar="ORT",
                    help="Zentrum der Discovery-Umkreissuche (Standard: Bonn)")
    ap.add_argument("--discover-umkreis", type=int, default=50, metavar="KM",
                    help="Radius der Discovery-Suche in km (Standard: 50)")
    ap.add_argument("--discover-query", action="append", default=[], metavar="Q",
                    help="Suchbegriff für --discover (mehrfach); Standard: Data/Analytics-Rollen")
    ap.add_argument("--no-state", action="store_true",
                    help="seen.json weder lesen noch schreiben (keine 🆕-Markierung)")
    ap.add_argument("--companies", default=str(COMPANIES_FILE), help="Pfad zur companies.json")
    args = ap.parse_args()

    keywords = load_keywords(args)
    excludes = load_excludes(args)

    firmen = json.loads(Path(args.companies).read_text(encoding="utf-8")).get("firmen", [])

    alle_jobs = scan_feeds(firmen)
    if args.discover:
        queries = args.discover_query or DEFAULT_DISCOVER_QUERIES
        alle_jobs.extend(discover_arbeitsagentur(queries, args.discover_ort, args.discover_umkreis))

    # Treffer-Schlagworte erfassen; ohne Treffer wird (außer bei --all) gefiltert.
    for j in alle_jobs:
        j["treffer_worte"] = matched_keywords(j, keywords)

    treffer = alle_jobs if args.all else [j for j in alle_jobs if j["treffer_worte"]]
    # Negativ-Filter: Stellen mit Ausschluss-Wort im Titel raus (außer --no-exclude).
    if excludes:
        treffer = [j for j in treffer if not is_excluded(j, excludes)]
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

    # Mehrfach gelistete Stellen (z.B. ATS-Feed + Arbeitsagentur) entfalten.
    treffer = dedupe(treffer)

    # Zustand: neue Stellen markieren und Sichtungen fortschreiben (außer --no-state).
    neu_count = 0
    if not args.no_state:
        seen = load_seen()
        heute_iso = date.today().isoformat()
        for j in treffer:
            k = job_key(j)
            j["neu"] = k not in seen
            if j["neu"]:
                neu_count += 1
            seen.setdefault(k, heute_iso)
        save_seen(seen)

    # Reihenfolge = Scan-Lauf (companies.json-Reihenfolge × Stellen je Feed),
    # bewusst NICHT alphabetisch sortiert.

    print(f"\n{len(treffer)} Treffer ({neu_count} 🆕 neu) von {len(alle_jobs)} Stellen gesamt:\n",
          file=sys.stderr)
    print_table(treffer)
    write_markdown(treffer, args)
    print(f"\n→ Markdown geschrieben: {OUTPUT_MD}", file=sys.stderr)


if __name__ == "__main__":
    main()

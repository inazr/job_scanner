#!/usr/bin/env python3
"""
Slug-Finder: ergänzt zu einem Firmennamen automatisch platform + slug.

Strategie (zweistufig):
  1. RATEN  – Slug-Kandidaten aus dem Namen bilden und alle Plattform-Feeds
             testen. Ein Kandidat gilt nur als Treffer, wenn der Feed echte
             Stellen liefert (die Fetcher aus scan_jobs.py sind die Verifikation).
  2. SUCHE  – nur falls Raten nichts findet: DuckDuckGo nach der Karriereseite
             durchsuchen und den Slug aus den Ergebnis-URLs extrahieren,
             anschließend wieder per Feed verifizieren.

Gefundene Treffer werden (nach Bestätigung) in companies.json eingetragen.

Beispiele:
  python3 find_slug.py "HERO Software"      # einen Namen auflösen + eintragen
  python3 find_slug.py "FINN" --yes         # ohne Rückfrage eintragen
  python3 find_slug.py                       # alle name-only-Einträge in
                                             # companies.json auflösen
"""

import argparse
import json
import re
import sys
import urllib.parse

# Fetcher + Pfade aus dem Scanner wiederverwenden (kein __main__ läuft beim Import).
from scan_jobs import FETCHERS, _get, COMPANIES_FILE

# Rechtsform-/Füllwörter, die nicht Teil des Slugs sind.
LEGAL = {
    "gmbh", "mbh", "ag", "se", "kg", "kgaa", "ohg", "ug", "co", "company",
    "inc", "ltd", "limited", "llc", "plc", "corp", "bv", "nv",
}

# ATS-URL-Muster → Plattform. Gruppe 1 ist jeweils der Slug.
PATTERNS = [
    (r"([a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)", "personio"),
    (r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9-]*)", "ashby"),
    (r"posting-api/job-board/([a-z0-9][a-z0-9-]*)", "ashby"),
    (r"jobs\.lever\.co/([a-z0-9][a-z0-9-]*)", "lever"),
    (r"apply\.workable\.com/([a-z0-9][a-z0-9-]*)", "workable"),
    (r"([a-z0-9][a-z0-9-]*)\.workable\.com", "workable"),
    (r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9][a-z0-9-]*)", "greenhouse"),
    (r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9][a-z0-9-]*)", "greenhouse"),
    (r"api\.smartrecruiters\.com/v1/companies/([a-z0-9][a-z0-9-]*)", "smartrecruiters"),
    (r"jobs\.smartrecruiters\.com/([a-z0-9][a-z0-9-]*)", "smartrecruiters"),
    (r"([a-z0-9][a-z0-9-]*)\.recruitee\.com", "recruitee"),
]

# Pfad-/Subdomain-Segmente, die nie ein Firmen-Slug sind.
BLOCK = {
    "www", "apply", "api", "j", "jobs", "careers", "career", "posting-api",
    "widget", "accounts", "account", "de", "com", "en", "spi",
    "boards", "embed", "companies", "postings", "v1", "job-board",
}

# Wird von resolve() gesetzt: True, wenn die Websuche blockiert war (= [] ist
# dann ein falsches Negativ, kein echtes "nicht vorhanden").
LAST_SEARCH_BLOCKED = False


def _translit(s):
    table = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "è": "e", "á": "a"}
    return "".join(table.get(c, c) for c in s)


def candidate_slugs(name):
    """Plausible Slug-Kandidaten aus einem Firmennamen ableiten.

    Deckt auch untypische ATS-Slug-Formen ab, die reines Raten sonst verfehlt
    (beobachtet: 'taxfix.com' bei Ashby, 'quantco-' bei Lever, 'SennderGmbH'
    bei SmartRecruiters). Punkt und nachgestellter Bindestrich bleiben erhalten.
    """
    base = _translit(name.lower())
    base = re.sub(r"[&/+.,']", " ", base)
    tokens = [t for t in re.split(r"\s+", base) if t and t not in LEGAL]
    if not tokens:
        tokens = [re.sub(r"[^a-z0-9]", "", base)]

    cands = []

    def add(s):
        s = re.sub(r"[^a-z0-9.-]", "", s)
        s = re.sub(r"-{2,}", "-", s).strip(".").lstrip("-")
        if s and s not in cands:
            cands.append(s)

    conc = "".join(tokens)
    add("-".join(tokens))      # hero-software
    add(conc)                  # herosoftware
    if len(tokens) > 1:
        add(tokens[0])         # hero
        add("-".join(tokens[:2]))
        add("".join(tokens[:2]))
    # Suffix-Varianten für untypische ATS-Slugs
    for suf in (".com", "-", "gmbh", "-gmbh", "hq", "group"):
        add(conc + suf)        # taxfix.com / quantco- / senndergmbh / …
    return cands


def _verify(name, platform, slug):
    """Feed abrufen; Stellenzahl bei Erfolg, sonst None."""
    try:
        jobs = FETCHERS[platform](name, slug)
    except Exception:
        return None
    return len(jobs) if jobs else None


def _fetch_jobs(name, platform, slug):
    """Feed abrufen; Stellen-Liste bei Erfolg, sonst None."""
    try:
        jobs = FETCHERS[platform](name, slug)
    except Exception:
        return None
    return jobs or None


def _verdacht(name, slug, jobs):
    """Kollisions-Bremse: Gründe, warum ein Treffer eine FALSCHE Firma sein könnte
    (leere Liste = unauffällig). Verhindert Fehleinträge wie pure/swk/node."""
    gruende = []
    toks = [t for t in re.split(r"[^a-z0-9]+", _translit(name.lower())) if t and t not in LEGAL]
    # (1) generischer Kurz-Slug: nur erster (kurzer) Namensteil, ohne Bindestrich/Punkt
    if len(toks) >= 2 and "-" not in slug and "." not in slug and slug == toks[0] and len(slug) <= 5:
        gruende.append(f"generischer Kurz-Slug '{slug}' (nur erster Namensteil von '{name}')")
    # (2) Slug ohne Bezug zum Namen (typisch für Such-Fehltreffer/Übernahmen, z.B. cognigy->nice)
    slug_core = re.sub(r"(\.com|-?gmbh|-)$", "", slug)
    if toks and not any(t in slug or t in slug_core or slug_core in t for t in toks):
        gruende.append(f"Slug '{slug}' ohne Bezug zum Firmennamen")
    # (3) keine einzige Stelle in DACH/Europa und nichts remote -> wohl fremde Firma
    try:
        from scan_jobs import _standort_klasse
        if jobs and all(_standort_klasse(j) == "noneu" and not j.get("remote") for j in jobs):
            gruende.append("alle Stellen außerhalb Europas")
    except Exception:
        pass
    return gruende


# Such-Engines werden aus dieser Umgebung häufig geblockt (Stub-Seite ohne
# Ergebnis-Links). Wir versuchen es best-effort über mehrere Endpunkte und
# erkennen den Block, statt still ein falsches Negativ zu liefern.
_SEARCH_ENGINES = [
    "https://lite.duckduckgo.com/lite/?q=",
    "https://html.duckduckgo.com/html/?q=",
]


def _search_slugs(name):
    """Web-Fallback: (platform, slug) aus Such-URLs ziehen.

    Rückgabe: (treffer, blockiert). 'blockiert' ist True, wenn keine Engine eine
    brauchbare Ergebnisseite lieferte (dann ist [] ein falsches Negativ, kein
    echtes 'nicht vorhanden').
    """
    found = []
    brauchbar = False
    for q in (f"{name} careers", f"{name} jobs"):
        for base in _SEARCH_ENGINES:
            try:
                raw = _get(base + urllib.parse.quote_plus(q)).decode("utf-8", "ignore")
            except Exception:
                continue
            text = urllib.parse.unquote(raw)
            # Echte Ergebnisseiten enthalten Result-Links; Stub-/Block-Seiten nicht.
            if "uddg=" in text or "result__a" in text or "result-link" in text:
                brauchbar = True
            for pat, platform in PATTERNS:
                for m in re.finditer(pat, text, re.I):
                    slug = m.group(1).lower()
                    if slug not in BLOCK:
                        found.append((platform, slug))
    return list(dict.fromkeys(found)), brauchbar


def resolve(name):
    """Verifizierte Treffer für einen Firmennamen, beste (meiste Stellen) zuerst.
    Jeder Treffer trägt 'verdacht' (Kollisions-Hinweise; leere Liste = unauffällig)."""
    hits = {}

    def consider(platform, slug, quelle):
        if (platform, slug) in hits:
            return
        jobs = _fetch_jobs(name, platform, slug)
        if not jobs:
            return
        hits[(platform, slug)] = {
            "platform": platform, "slug": slug, "jobs": len(jobs),
            "quelle": quelle, "verdacht": _verdacht(name, slug, jobs),
        }

    # Phase 1: raten
    for slug in candidate_slugs(name):
        for platform in FETCHERS:
            consider(platform, slug, "raten")

    # Phase 2: Websuche nur, wenn Raten leer blieb (häufig geblockt)
    global LAST_SEARCH_BLOCKED
    LAST_SEARCH_BLOCKED = False
    if not hits:
        such, brauchbar = _search_slugs(name)
        LAST_SEARCH_BLOCKED = not brauchbar
        for platform, slug in such:
            consider(platform, slug, "suche")

    return sorted(hits.values(), key=lambda h: -h["jobs"])


def add_to_companies(name, hit, path):
    """platform+slug in companies.json setzen (Eintrag aktualisieren oder anlegen)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    firmen = data.setdefault("firmen", [])
    for f in firmen:
        if f.get("name", "").lower() == name.lower():
            f["platform"], f["slug"] = hit["platform"], hit["slug"]
            break
    else:
        firmen.append({"name": name, "platform": hit["platform"], "slug": hit["slug"]})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _choose(hits, auto):
    if len(hits) == 1 or auto:
        return hits[0]
    print("  Mehrere Treffer:", file=sys.stderr)
    for i, h in enumerate(hits, 1):
        warn = "  ⚠ " + "; ".join(h["verdacht"]) if h.get("verdacht") else ""
        print(f"    [{i}] {h['platform']} / {h['slug']} ({h['jobs']} Stellen, {h['quelle']}){warn}",
              file=sys.stderr)
    ans = input("  Welcher? [1]: ").strip() or "1"
    try:
        return hits[int(ans) - 1]
    except (ValueError, IndexError):
        return None


def process_one(name, path, auto):
    print(f"→ {name}: suche …", file=sys.stderr)
    hits = resolve(name)
    if not hits:
        if LAST_SEARCH_BLOCKED:
            print("  ⚠ kein Slug per Raten gefunden; Websuche blockiert – "
                  "Slug ggf. manuell suchen (jobs.ashbyhq.com/<x>, boards.greenhouse.io/<x> …).",
                  file=sys.stderr)
        else:
            print("  ⚠ kein Slug gefunden – bitte manuell in companies.json eintragen.",
                  file=sys.stderr)
        return False
    for h in hits:
        warn = "  ⚠ Verdacht: " + "; ".join(h["verdacht"]) if h.get("verdacht") else ""
        print(f"  gefunden: {h['platform']} / {h['slug']} ({h['jobs']} Stellen, {h['quelle']}){warn}",
              file=sys.stderr)
    if auto:
        # Kollisions-Bremse: im --yes-Modus nur UNVERDÄCHTIGE Treffer automatisch eintragen.
        sauber = [h for h in hits if not h.get("verdacht")]
        if not sauber:
            print("  ⚠ nur verdächtige Treffer – nichts automatisch eingetragen (bitte manuell prüfen).",
                  file=sys.stderr)
            return False
        pick = sauber[0]
    else:
        pick = _choose(hits, False)
        if not pick:
            print("  übersprungen.", file=sys.stderr)
            return False
        if input(f"  Eintragen als {pick['platform']}/{pick['slug']}? [j/n]: ").strip().lower() not in ("j", "ja", "y", ""):
            print("  übersprungen.", file=sys.stderr)
            return False
    add_to_companies(name, pick, path)
    print(f"  ✓ eingetragen: {pick['platform']} / {pick['slug']}", file=sys.stderr)
    return True


def main():
    ap = argparse.ArgumentParser(description="Findet platform+slug zu einem Firmennamen.")
    ap.add_argument("name", nargs="?", help="Firmenname. Ohne Angabe: alle name-only-Einträge auflösen.")
    ap.add_argument("--yes", "-y", action="store_true", help="ohne Rückfrage eintragen")
    ap.add_argument("--companies", default=str(COMPANIES_FILE), help="Pfad zur companies.json")
    args = ap.parse_args()

    from pathlib import Path
    path = Path(args.companies)

    if args.name:
        ok = process_one(args.name, path, args.yes)
        sys.exit(0 if ok else 1)

    # Ohne Namen: alle Einträge ohne platform/slug auflösen.
    firmen = json.loads(path.read_text(encoding="utf-8")).get("firmen", [])
    offen = [f.get("name") for f in firmen if f.get("name") and not (f.get("platform") and f.get("slug"))]
    if not offen:
        print("Keine offenen Einträge (alle haben platform + slug).", file=sys.stderr)
        return
    print(f"{len(offen)} offene Einträge werden aufgelöst …\n", file=sys.stderr)
    for name in offen:
        process_one(name, path, args.yes)


if __name__ == "__main__":
    main()

"""Plain-Python logic behind the Tk windows (no tkinter, so tests can import it):
arguments and the first page, the picker's filter, and small formatters."""
import argparse
import re
import unicodedata

from ..settings import logic

PAGES = ("places", "appearance", "motion", "clock", "general", "about")
TITLES = {"places": "Places", "appearance": "Appearance", "motion": "Motion",
          "clock": "Clock & Weather", "general": "General", "about": "About"}
# GTK pages this window doesn't have -> where their content lives here
GTK_ONLY = {"displays": "general", "welcome": "clock"}


def parse_args(argv):
    """The same options as topopaper/settings/app.py, so callers can't tell them apart."""
    ap = argparse.ArgumentParser(prog="topopaper-settings",
                                 description="Settings for the topopaper wallpaper")
    ap.add_argument("--page", choices=PAGES + tuple(GTK_ONLY), help="open this page")
    ap.add_argument("--search", metavar="PLACE", help="look up a place on the Places page")
    ap.add_argument("--step", help=argparse.SUPPRESS)       # GTK welcome step; ignored
    return ap.parse_args(argv)


def first_page(page=None, search=None, config_exists=True):
    """The page to open. Before config.ini exists the user starts on Clock &
    Weather to set their location (this window has no welcome flow)."""
    if search:
        return "places"
    if not config_exists:
        return "clock"
    page = GTK_ONLY.get(page, page)
    return page if page in PAGES else "places"


# ---- places ---------------------------------------------------------------------
def can_delete(name, home="", current=""):
    """Never the globe, the home map or the map on screen."""
    return bool(name) and name not in ("earth", home, current)


def job_subtitle(job):
    """'Building the map…', 'Ready', 'Failed — build failed'."""
    state = job.get("state", "running")
    stage = (job.get("stage") or "").strip()
    if state == "done":
        return "Ready"
    if state == "failed":
        return f"Failed — {stage}" if stage else "Failed"
    return (stage[:1].upper() + stage[1:] + "…") if stage else "Starting…"


def maps_summary(shown, hidden_connectors):
    out = f"{shown} map{'s' if shown != 1 else ''}"
    if hidden_connectors:
        out += (f" · {hidden_connectors} connector map"
                f"{'s' if hidden_connectors != 1 else ''} hidden")
    return out


def parse_coords(lat, lon):
    """Two text fields -> (lat, lon) floats; ValueError with a readable message."""
    try:
        la, lo = float(str(lat).strip().replace(",", ".")), float(str(lon).strip().replace(",", "."))
    except ValueError:
        raise ValueError("Latitude and longitude must be numbers, e.g. 46.02 and 7.75") from None
    if not -90 <= la <= 90:
        raise ValueError("Latitude must be between -90 and 90")
    if not -180 <= lo <= 180:
        raise ValueError("Longitude must be between -180 and 180")
    return la, lo


# ---- the picker -----------------------------------------------------------------
def fold(s):
    """Lower case, accents off, punctuation to spaces: 'Saas-Fée' -> 'saas fee'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def picker_items(packs, current=""):
    """util.list_packs() output -> [dict(name, title, scale, current)] for the
    picker: real places only (the connector rungs are plumbing), the one on
    screen first."""
    out = []
    for name, meta in packs:
        meta = meta or {}
        if meta.get("scaffold") and name != current:
            continue
        out.append(dict(name=name, title=logic.pack_title(name, meta),
                        scale=logic.scale_label(logic.tier_of(meta)),
                        current=(name == current)))
    out.sort(key=lambda it: (not it["current"], it["name"] != "earth", it["title"].lower()))
    return out


def _is_subsequence(q, h):
    it = iter(h)
    return all(c in it for c in q)


def match_score(query, item):
    """How well `query` (already folded) matches an item; 0 = not at all."""
    best = 0
    for hay in (fold(item.get("title", "")), fold(item.get("name", ""))):
        words = hay.split()
        qwords = query.split()
        if hay == query:
            s = 100
        elif hay.startswith(query):
            s = 80
        elif any(w.startswith(query) for w in words):
            s = 65
        elif qwords and all(any(w.startswith(q) for w in words) for q in qwords):
            s = 55
        elif query in hay:
            s = 40
        elif len(query) >= 3 and _is_subsequence(query.replace(" ", ""), hay.replace(" ", "")):
            s = 10
        else:
            s = 0
        best = max(best, s)
    return best


def rank(query, items):
    """The items matching `query`, best first (ties keep their order). An
    empty query returns everything."""
    q = fold(query)
    if not q:
        return list(items)
    scored = [(-match_score(q, it), i, it) for i, it in enumerate(items)]
    return [it for s, _i, it in sorted(scored, key=lambda x: (x[0], x[1])) if s < 0]

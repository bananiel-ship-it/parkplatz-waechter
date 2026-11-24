#!/usr/bin/env python3
"""Parkplatz-Wächter

Sucht neue Mietparkplätze (Flatfox, tutti.ch) in Gehdistanz und meldet sie per ntfy-Push.

Umgebungsvariablen:
  HOME_LAT, HOME_LON  Koordinaten der Wohnadresse (Pflicht)
  NTFY_TOPIC          geheimer ntfy-Kanal (Pflicht, ausser bei --dry-run)
  NTFY_SERVER         Standard: https://ntfy.sh
  ANFRAGE_SEITE       URL der Anfragetext-Seite (optional, z. B. GitHub Pages)
  STATE_PATH          Standard: state/state.json

Aufruf:
  python3 waechter.py             normaler Lauf
  python3 waechter.py --dry-run   nichts senden, nichts speichern, Treffer anzeigen
  python3 waechter.py --test-push Beispiel-Push senden
"""
import base64
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; parkplatz-waechter/1.0)"
TZ = ZoneInfo("Europe/Zurich")

TYPES = {
    "tg":  {"label": "Tiefgarage",   "akk": "Tiefgaragenplatz", "covered": True},
    "box": {"label": "Einzelgarage", "akk": "Garagenplatz",     "covered": True},
    "cp":  {"label": "Carport",      "akk": "Carport-Platz",    "covered": True},
    "out": {"label": "Aussenplatz",  "akk": "Parkplatz",        "covered": False},
}

SKIP_RE = re.compile(r"motorrad|motorad|motorbike|\bmoto\b|velo|fahrrad|\bbike|roller|scooter|wohnmobil|camper|\bboot", re.I)
BOX_RE = re.compile(r"einzelgarage|garagenbox|doppelgarage|\bbox\b|autobox", re.I)
TG_RE = re.compile(r"tiefgarage|einstellhalle|einstellplatz|garagenplatz|garagen-platz|untergrund|unterirdisch|parkhaus|autoeinstell|garage", re.I)
CP_RE = re.compile(r"carport|überdacht|ueberdacht|gedeckt", re.I)
PARK_RE = re.compile(r"parkplatz|parkplätze|abstellplatz|stellplatz|einstellplatz|tiefgarage|garage|carport|einstellhalle|autoabstell|parking", re.I)


# ---------------------------------------------------------------- Hilfen

# Auf GitHub sind die Logs öffentlich: dort keine Treffer mit Gehzeiten ausgeben,
# sonst liesse sich der Wohnort daraus ableiten.
PUBLIC_LOG = os.environ.get("GITHUB_ACTIONS") == "true"


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def http(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept-Language": "de-CH,de;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        # Ältere macOS-Pythons scheitern an manchen TLS-Servern; dann curl nehmen.
        if data is not None or "SSL" not in str(e.reason):
            raise
        cmd = ["curl", "-sfL", "--max-time", str(timeout)]
        for k, v in h.items():
            cmd += ["-H", "%s: %s" % (k, v)]
        return subprocess.run(cmd + [url], check=True, capture_output=True).stdout.decode("utf-8", "replace")


def get_json(url):
    return json.loads(http(url, headers={"Accept": "application/json"}))


def airline_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def classify(title, body="", hint=None):
    """Platzart bestimmen: tg / box / cp / out, None = kein Autoparkplatz."""
    if SKIP_RE.search(title or ""):
        return None
    if hint in ("tg", "box", "cp"):
        return hint
    for text in (title or "", (title or "") + " " + (body or "")):
        if BOX_RE.search(text):
            return "box"
        if TG_RE.search(text):
            return "tg"
        if CP_RE.search(text):
            return "cp"
    return "out"


def signature(l):
    street = re.sub(r"[^a-z0-9]", "", (l.get("street") or "").lower())[:18]
    return "%s|%s" % (street, l.get("price"))


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    try:
        cfg["home"] = {"lat": float(os.environ["HOME_LAT"]), "lon": float(os.environ["HOME_LON"])}
    except (KeyError, ValueError):
        sys.exit("HOME_LAT und HOME_LON fehlen oder sind ungültig.")
    return cfg


def state_path():
    return os.environ.get("STATE_PATH", os.path.join(ROOT, "state", "state.json"))


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("seen", {})
    s.setdefault("sigs", {})
    s.setdefault("queue", [])
    s.setdefault("fails", {})
    return s


def save_state(s):
    cutoff = time.time() - 120 * 86400
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cutoff}
    s["sigs"] = {k: v for k, v in s["sigs"].items() if v > cutoff}
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------------------------------------------------------------- Quellen

FLATFOX_TYPES = [("BIKE", None), ("MOTO", None), ("GARAGE_SLOT", "tg"), ("UNDERGROUND", "tg"),
                 ("GARAGE", "box"), ("COVERED", "cp"), ("CARPORT", "cp"), ("OPEN", "out")]


def flatfox_from(x):
    t = x.get("moving_date_type")
    if t == "imm":
        return "sofort"
    if x.get("moving_date"):
        try:
            return datetime.strptime(x["moving_date"][:10], "%Y-%m-%d").strftime("%-d.%-m.%Y")
        except ValueError:
            pass
    return "nach Vereinbarung"


def source_flatfox(cfg, state):
    h = cfg["home"]
    m = cfg["max_gehzeit_min"] * 80 + 200
    dlat = m / 111320
    dlon = m / (111320 * math.cos(math.radians(h["lat"])))
    q = urllib.parse.urlencode({
        "east": round(h["lon"] + dlon, 5), "west": round(h["lon"] - dlon, 5),
        "north": round(h["lat"] + dlat, 5), "south": round(h["lat"] - dlat, 5),
        "max_count": 400, "object_category": "PARK",
    })
    pins = get_json("https://flatfox.ch/api/v1/pin/?" + q)
    new = [p["pk"] for p in pins if "flatfox:%s" % p["pk"] not in state["seen"] and not p.get("selling_price")]
    out = []
    for i in range(0, len(new), 40):
        chunk = new[i:i + 40]
        url = "https://flatfox.ch/api/v1/public-listing/?" + "&".join("pk=%d" % pk for pk in chunk) + "&limit=%d" % len(chunk)
        for x in get_json(url).get("results", []):
            if x.get("offer_type") != "RENT":
                state["seen"]["flatfox:%s" % x["pk"]] = time.time()
                continue
            otype = (x.get("object_type") or "").upper()
            hint = "?"
            for key, val in FLATFOX_TYPES:
                if key in otype:
                    hint = val
                    break
            title = x.get("description_title") or x.get("short_title") or ""
            typ = None if hint is None else classify(title, x.get("description") or "", None if hint in ("?", "out") else hint)
            out.append({
                "key": "flatfox:%s" % x["pk"], "source": "Flatfox", "type": typ, "title": title,
                "street": x.get("street") or x.get("public_address") or "Adresse auf Anfrage",
                "zip": x.get("zipcode"), "lat": x.get("latitude"), "lon": x.get("longitude"),
                "price": x.get("price_display"), "from": flatfox_from(x),
                "url": "https://flatfox.ch" + (x.get("url") or "/%s/" % x["pk"]).replace("/en/", "/de/", 1),
            })
    return out


def tutti_listings(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("tutti: Seitenformat hat sich geändert")
    found = []

    def walk(o):
        if isinstance(o, dict):
            L = o.get("listings")
            if isinstance(L, dict) and "edges" in L:
                found.extend(e["node"] for e in L["edges"] if "node" in e)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(json.loads(m.group(1)))
    return found


def tutti_price(s):
    m = re.search(r"([\d']+)\.-", s or "")
    return int(m.group(1).replace("'", "")) if m else None


def tutti_from(body):
    m = re.search(r"\b(?:ab|per)\s+(sofort|\d{1,2}\.\s?\d{1,2}\.(?:\s?\d{2,4})?)", body or "", re.I)
    if not m:
        return "nach Vereinbarung"
    v = m.group(1).replace(" ", "")
    return "sofort" if v.lower() == "sofort" else v


def source_tutti(cfg, state):
    plz = set(cfg["tutti"]["plz"])
    cand = {}
    for kw in cfg["tutti"]["suchbegriffe"]:
        html = http("https://www.tutti.ch/de/q/suche?" + urllib.parse.urlencode({"query": kw}))
        for n in tutti_listings(html):
            key = "tutti:%s" % n.get("listingID")
            if key in state["seen"] or key in cand:
                continue
            loc = n.get("localization") or {}
            title, body = loc.get("title") or "", loc.get("body") or ""
            pc = (n.get("postcodeInformation") or {}).get("postcode")
            price_s = n.get("formattedPrice") or ""
            if not pc or int(pc) not in plz or "pro Monat" not in price_s or not PARK_RE.search(title + " " + body):
                state["seen"][key] = time.time()
                continue
            cand[key] = n
        time.sleep(1)
    out = []
    for key, n in cand.items():
        lid = n["listingID"]
        url = "https://www.tutti.ch/de/vi/%s" % lid
        lat = lon = None
        body = (n.get("localization") or {}).get("body") or ""
        try:
            page = http(url)
            m = re.search(r'"coordinates":\s*\{"latitude":\s*([\d.]+),\s*"longitude":\s*([\d.]+)\}', page)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
            mb = re.search(r'"body":\s*"((?:[^"\\]|\\.)*)"', page)
            if mb:
                body = json.loads('"%s"' % mb.group(1))
        except Exception as e:  # Detailseite optional
            log("tutti Detail", lid, e)
        time.sleep(1)
        title = (n.get("localization") or {}).get("title") or ""
        pc = (n.get("postcodeInformation") or {}).get("postcode")
        out.append({
            "key": key, "source": "tutti.ch", "type": classify(title, body), "title": title,
            "street": "%s Zürich (Inserat)" % pc, "zip": pc, "lat": lat, "lon": lon,
            "price": tutti_price(n.get("formattedPrice")), "from": tutti_from(body), "url": url,
        })
    return out


SOURCES = [("Flatfox", source_flatfox), ("tutti.ch", source_tutti)]


# ---------------------------------------------------------------- Gehzeit

def add_walk(cfg, items):
    h = cfg["home"]
    limit = cfg["max_gehzeit_min"] * 80  # Luftlinie darüber kann nicht unter dem Maximum liegen
    todo = []
    for l in items:
        l["walk"] = None
        if l.get("lat") is None or l.get("lon") is None or l.get("type") is None:
            continue
        d = airline_m(h["lat"], h["lon"], l["lat"], l["lon"])
        l["airline"] = d
        if d <= limit:
            todo.append(l)
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        coords = ";".join(["%f,%f" % (h["lon"], h["lat"])] + ["%f,%f" % (l["lon"], l["lat"]) for l in chunk])
        try:
            r = get_json("https://routing.openstreetmap.de/routed-foot/table/v1/foot/%s?sources=0&annotations=duration" % coords)
            durs = r["durations"][0][1:]
        except Exception as e:
            log("Routing nicht erreichbar, schätze über Luftlinie:", e)
            durs = [None] * len(chunk)
        for l, d in zip(chunk, durs):
            if d is None:
                d = l["airline"] * 1.35 / 75 * 60
            l["walk"] = max(1, int(round(d / 60)))


def passes(cfg, l):
    if l.get("type") is None or l.get("walk") is None:
        return False
    if not cfg["platzarten"].get(l["type"], False):
        return False
    if l["walk"] > cfg["max_gehzeit_min"]:
        return False
    if l.get("price") is not None and l["price"] > cfg["max_miete_chf"]:
        return False
    return True


def rank(cfg, l):
    covered = TYPES[l["type"]]["covered"]
    return (0 if covered else 1, 0 if l["walk"] <= cfg["wunsch_gehzeit_min"] else 1, l["walk"])


# ---------------------------------------------------------------- Push

def ntfy(payload, dry):
    if dry:
        log("PUSH (nicht gesendet):", json.dumps(payload, ensure_ascii=False))
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        sys.exit("NTFY_TOPIC fehlt.")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = dict(payload, topic=topic)
    http(server, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})


def price_text(l):
    return "CHF %s/Mt." % l["price"] if l.get("price") is not None else "Preis auf Anfrage"


def anfrage_url(l):
    page = os.environ.get("ANFRAGE_SEITE")
    if not page:
        return None
    data = {"a": TYPES[l["type"]]["akk"], "s": l["street"], "f": l["from"], "u": l["url"]}
    tok = base64.urlsafe_b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode().rstrip("=")
    return page + "#" + tok


def push_listing(cfg, l, dry):
    t = TYPES[l["type"]]
    far = l["walk"] > cfg["wunsch_gehzeit_min"]
    title = "Neu: %s, %d Min. zu Fuss%s" % (t["label"], l["walk"], ", etwas weiter" if far else "")
    msg = "%s · %s · ab %s\n%s" % (l["street"], price_text(l), l["from"], l["source"])
    if l["from"] in ("sofort", "nach Vereinbarung"):
        msg = "%s · %s · %s\n%s" % (l["street"], price_text(l), "ab sofort" if l["from"] == "sofort" else "nach Vereinbarung", l["source"])
    actions = [{"action": "view", "label": "Inserat öffnen", "url": l["url"], "clear": True}]
    au = anfrage_url(l)
    if au:
        actions.append({"action": "view", "label": "Anfragetext", "url": au})
    ntfy({
        "title": title, "message": msg, "click": l["url"], "actions": actions,
        "priority": (5 if not far else 4) if t["covered"] else 3,
        "tags": ["blue_car"] if t["covered"] else ["parking"],
    }, dry)


def liste_url(cfg, title, items):
    """Link auf docs/liste.html: jedes Inserat einzeln antippbar."""
    page = os.environ.get("ANFRAGE_SEITE")
    if not page or not items:
        return None
    data = {"t": title, "x": cfg["wunsch_gehzeit_min"], "i": [{
        "k": TYPES[l["type"]]["label"], "a": TYPES[l["type"]]["akk"], "c": TYPES[l["type"]]["covered"],
        "w": l["walk"], "p": price_text(l), "s": l["street"], "f": l["from"], "q": l["source"], "u": l["url"],
    } for l in items[:30]]}
    tok = base64.urlsafe_b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode().rstrip("=")
    return urllib.parse.urljoin(page, "liste.html") + "#" + tok


def push_summary(cfg, title, items, dry, prio=3):
    if not items:
        return
    lines = ["%s, %d Min., %s, %s" % (TYPES[l["type"]]["label"], l["walk"], price_text(l), l["street"]) for l in items[:10]]
    if len(items) > 10:
        lines.append("… und %d weitere" % (len(items) - 10))
    lu = liste_url(cfg, title, items)
    if lu:
        lines.append("Antippen zeigt alle Inserate einzeln.")
    actions = [{"action": "view", "label": "Alle ansehen", "url": lu}] if lu else []
    actions.append({"action": "view", "label": "Bester Treffer", "url": items[0]["url"]})
    ntfy({
        "title": title, "message": "\n".join(lines), "priority": prio, "tags": ["parking"],
        "click": lu or items[0]["url"], "actions": actions,
    }, dry)


def test_push(dry):
    sample = {"type": "tg", "walk": 4, "street": "Beispielstrasse 1", "price": 190, "from": "sofort",
              "source": "Test", "url": "https://flatfox.ch/de/search/?object_category=PARK"}
    push_listing({"wunsch_gehzeit_min": 7}, sample, dry)


# ---------------------------------------------------------------- Ablauf

def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    if "--test-push" in args:
        test_push(dry)
        log("Test-Push gesendet.")
        return
    cfg = load_config()
    state = load_state()
    first = not state.get("initialized")
    now = datetime.now(TZ)
    n = cfg["nachtruhe"]
    night = now.hour >= n["von_uhr"] or now.hour < n["bis_uhr"]

    found = []
    for name, fn in SOURCES:
        try:
            items = fn(cfg, state)
            log("%s: %d neue Parkplatz-Inserate" % (name, len(items)))
            found += items
            state["fails"][name] = 0
        except Exception as e:
            if getattr(e, "code", None) == 403:
                # Portal sperrt diese Server (z. B. GitHub). Nicht umgehen, nicht warnen.
                log("%s: gesperrt (403), übersprungen" % name)
                continue
            c = state["fails"].get(name, 0) + 1
            state["fails"][name] = c
            log("%s: Fehler (%dx hintereinander): %s" % (name, c, e))
            if c == 12:  # rund 2 Stunden
                ntfy({"title": "Parkplatz-Wächter: %s antwortet nicht" % name,
                      "message": "Seit etwa 2 Stunden klappt die Abfrage nicht: %s" % e, "priority": 3,
                      "tags": ["warning"]}, dry)

    add_walk(cfg, found)
    matches = sorted([l for l in found if passes(cfg, l)], key=lambda l: rank(cfg, l))
    fresh = []
    for l in matches:
        sig = signature(l)
        if sig in state["sigs"]:
            if not PUBLIC_LOG:
                log("Doppelt, kein Push:", l["key"], l["street"])
            continue
        state["sigs"][sig] = time.time()
        fresh.append(l)
    if "--alle" in args:
        # Einmalig alle aktuell passenden Plätze als Liste schicken, Zustand bleibt unverändert.
        push_summary(cfg, "Aktuell %d passende Parkplätze" % len(matches), matches, dry)
        log("Liste mit %d Plätzen gesendet." % len(matches))
        return
    for l in found:
        state["seen"][l["key"]] = time.time()
        if l in matches and not PUBLIC_LOG:
            log("TREFFER %-12s %2d Min.  %-18s %-40s %s" % (TYPES[l["type"]]["label"], l["walk"], price_text(l), l["street"][:40], l["url"]))

    if first:
        if fresh:
            push_summary(cfg, "Parkplatz-Wächter läuft: %d passende Plätze sind schon ausgeschrieben" % len(fresh), fresh, dry)
        else:
            ntfy({"title": "Parkplatz-Wächter läuft", "message": "Gerade passt kein Inserat. Ich melde mich, sobald eines kommt.",
                  "priority": 3, "tags": ["parking"]}, dry)
        state["initialized"] = True
    else:
        now_push, queued = [], []
        for l in fresh:
            if night and n["nachts_nur_ueberdacht"] and not TYPES[l["type"]]["covered"]:
                queued.append(l)
            else:
                now_push.append(l)
        limit = cfg["max_einzel_pushes_pro_lauf"]
        for l in now_push[:limit]:
            push_listing(cfg, l, dry)
        if len(now_push) > limit:
            push_summary(cfg, "%d weitere neue Parkplätze" % (len(now_push) - limit), now_push[limit:], dry)
        state["queue"] += queued
        if not night and state["queue"]:
            q = state["queue"]
            push_summary(cfg, "%d Aussenplätze aus der Nacht" % len(q), q, dry)
            state["queue"] = []

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if not dry:
        save_state(state)
    log("Fertig: %d Treffer, davon %d neu." % (len(matches), len(fresh)))


if __name__ == "__main__":
    main()

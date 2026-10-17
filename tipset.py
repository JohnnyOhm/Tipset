#!/usr/bin/env python3
"""
tipset.py - Hämtar Stryktipset, Europatipset och Topptipset från Svenska Spel
och berikar varje match med tabell, form, inbördes möten och skadade spelare
från ESPN:s öppna (inofficiella) API. Ingen API-nyckel behövs.

Användning:
    py -m pip install requests
    py tipset.py                  # alla tre spelen
    py tipset.py stryktipset      # bara ett spel
    py tipset.py --no-stats       # bara kupongen
    py tipset.py --dump-raw       # spara rå-JSON (för felsökning)
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests

SVS_BASE = "https://api.spela.svenskaspel.se/draw/1"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer"
CACHE_DIR = Path(".cache")
RAW_DIR = Path("raw")

PRODUCTS = ["stryktipset", "europatipset", "topptipset"]
# Topptipset finns i flera varianter hos Svenska Spel; vi provar alla och tar de som svarar.
SVS_ENDPOINTS = {
    "stryktipset": ["stryktipset"],
    "europatipset": ["europatipset"],
    # topptipsetfamily samlar alla Topptipset-varianter (Topptipset, Europa, Extra ...)
    "topptipset": ["topptipsetfamily", "topptipseteuropa", "topptipsetstryk", "topptipsetextra"],
}

# Svenska Spels liganamn -> ESPN:s liga-id. Lägg till fler vid behov.
LEAGUES = {
    "premier league": "eng.1", "championship": "eng.2", "league one": "eng.3",
    "league two": "eng.4", "national league": "eng.5",
    "fa cup": "eng.fa", "fa cupen": "eng.fa", "ligacupen": "eng.league_cup", "efl cup": "eng.league_cup",
    "nations league": "uefa.nations", "champions league": "uefa.champions",
    "europa league": "uefa.europa", "conference league": "uefa.europa.conf",
    "vm kval": "fifa.worldq.uefa", "em kval": "uefa.euroq",
    "la liga": "esp.1", "laliga": "esp.1", "serie a": "ita.1", "serie b": "ita.2",
    "2 bundesliga": "ger.2", "bundesliga": "ger.1", "ligue 1": "fra.1",
    "eredivisie": "ned.1", "primeira liga": "por.1", "premiership": "sco.1",
    "allsvenskan": "swe.1", "superettan": "swe.2", "eliteserien": "nor.1",
    "superligaen": "den.1", "superliga": "den.1",
    # Namn som bara finns i ett land - hittas även om landet saknas eller stavas oväntat
    "premier division": "irl.1", "eerste divisie": "ned.2", "ligue 2": "fra.2", "3 liga": "ger.3",
    "segunda division": "esp.2", "laliga2": "esp.2", "laliga 2": "esp.2", "la liga 2": "esp.2", "hypermotion": "esp.2", "veikkausliiga": "fin.1", "liga portugal": "por.1",
    "primeira liga": "por.1", "super lig": "tur.1", "pro league": "bel.1", "jupiler": "bel.1",
    "liga mx": "mex.1", "mls": "usa.1", "a league": "aus.1",
    # Landslag
    "vanskapsmatch": "fifa.friendly", "vanskapslandskamp": "fifa.friendly", "friendly": "fifa.friendly",
    "internationella vanskap": "fifa.friendly",
}

# Liga per land (Svenska Spels landsnamn på svenska eller landskod -> liganamn -> ESPN-id).
# Slås upp före LEAGUES ovan. Lägg till fler vid behov - namnen skrivs utan å/ä/ö.
LEAGUES_BY_COUNTRY = {
    "england": {"premier league": "eng.1", "championship": "eng.2", "league one": "eng.3",
                "league two": "eng.4", "national league": "eng.5", "fa cup": "eng.fa",
                "ligacup": "eng.league_cup", "efl cup": "eng.league_cup", "carabao": "eng.league_cup",
                "efl trophy": "eng.trophy"},
    "skottland": {"premiership": "sco.1", "championship": "sco.2", "league one": "sco.3", "league two": "sco.4"},
    "irland": {"premier division": "irl.1", "first division": "irl.2"},
    "spanien": {"la liga": "esp.1", "laliga": "esp.1", "primera division": "esp.1",
                "segunda": "esp.2", "laliga2": "esp.2", "laliga 2": "esp.2", "la liga 2": "esp.2",
                "la liga2": "esp.2", "hypermotion": "esp.2"},
    "italien": {"serie a": "ita.1", "serie b": "ita.2"},
    "tyskland": {"2 bundesliga": "ger.2", "3 liga": "ger.3", "bundesliga": "ger.1"},
    "frankrike": {"ligue 1": "fra.1", "ligue 2": "fra.2"},
    "nederlanderna": {"eredivisie": "ned.1", "eerste divisie": "ned.2"},
    "portugal": {"primeira liga": "por.1", "liga portugal": "por.1"},
    "belgien": {"pro league": "bel.1", "jupiler": "bel.1", "first division a": "bel.1"},
    "turkiet": {"super lig": "tur.1"},
    "grekland": {"super league": "gre.1"},
    "schweiz": {"super league": "sui.1"},
    "osterrike": {"bundesliga": "aut.1"},
    "danmark": {"superliga": "den.1", "superligaen": "den.1", "1 division": "den.2"},
    "norge": {"eliteserien": "nor.1", "obos": "nor.2", "1 divisjon": "nor.2", "nm cup": "nor.cup"},
    "sverige": {"allsvenskan": "swe.1", "superettan": "swe.2", "svenska cupen": "swe.cup"},
    "finland": {"veikkausliiga": "fin.1"},
    "usa": {"mls": "usa.1", "major league soccer": "usa.1", "usl championship": "usa.usl.1", "usl": "usa.usl.1",
            "us open cup": "usa.open"},
    "brasilien": {"serie a": "bra.1", "serie b": "bra.2", "copa do brasil": "bra.copa_do_brazil"},
    "argentina": {"primera division": "arg.1", "liga profesional": "arg.1", "copa argentina": "arg.copa"},
    "mexiko": {"liga mx": "mex.1"},
    "japan": {"j league": "jpn.1", "j1": "jpn.1", "j2": "jpn.2"},
    "saudiarabien": {"pro league": "ksa.1"},
    "australien": {"a league": "aus.1"},
    "colombia": {"primera a": "col.1", "liga betplay": "col.1", "primera b": "col.2", "copa colombia": "col.copa"},
    "polen": {"ekstraklasa": "pol.1"},
    "tjeckien": {"liga": "cze.1"},
    "kroatien": {"hnl": "cro.1", "liga": "cro.1"},
    "serbien": {"superliga": "srb.1"},
    "rumanien": {"liga 1": "rou.1", "superliga": "rou.1"},
    "ryssland": {"premier": "rus.1"},
    "ukraina": {"premier": "ukr.1"},
    "israel": {"premier": "isr.1"},
    "cypern": {"division": "cyp.1"},
    "kina": {"super league": "chn.1"},
    "sydkorea": {"k league": "kor.1"},
    "chile": {"primera division": "chi.1"},
    "paraguay": {"primera": "par.1"},
    "venezuela": {"primera": "ven.1"},
    "bolivia": {"primera": "bol.1"},
    "costa rica": {"primera": "crc.1"},
    "egypten": {"premier": "egy.1"},
    "sydafrika": {"premier": "rsa.1"},
    "chile": {"primera division": "chi.1"},
    "uruguay": {"primera division": "uru.1"},
    "peru": {"liga 1": "per.1"},
    "ecuador": {"liga pro": "ecu.1", "serie a": "ecu.1"},
    "internationell": {"vanskap": "fifa.friendly", "friendly": "fifa.friendly",
                       "concacaf nations league": "concacaf.nations.league", "nations league": "uefa.nations",
                       "champions league": "uefa.champions", "europa league": "uefa.europa",
                       "conference league": "uefa.europa.conf", "copa libertadores": "conmebol.libertadores",
                       "copa sudamericana": "conmebol.sudamericana",
                       "vm kval": "fifa.worldq.uefa", "em kval": "uefa.euroq"},
}
COUNTRY_ALIASES = {  # landskoder -> svenska landsnamnet ovan
    "eng": "england", "sco": "skottland", "irl": "irland", "esp": "spanien", "ita": "italien",
    "ger": "tyskland", "deu": "tyskland", "fra": "frankrike", "ned": "nederlanderna", "nld": "nederlanderna",
    "por": "portugal", "prt": "portugal", "bel": "belgien", "tur": "turkiet", "gre": "grekland",
    "grc": "grekland", "sui": "schweiz", "che": "schweiz", "aut": "osterrike", "den": "danmark",
    "dnk": "danmark", "nor": "norge", "swe": "sverige", "fin": "finland", "us": "usa",
    "bra": "brasilien", "arg": "argentina", "mex": "mexiko", "jpn": "japan", "ksa": "saudiarabien",
    "sau": "saudiarabien", "aus": "australien", "republiken irland": "irland",
    # tvåbokstavskoder, om Svenska Spel använder dem
    "gb": "england", "ie": "irland", "es": "spanien", "it": "italien", "de": "tyskland", "fr": "frankrike",
    "nl": "nederlanderna", "pt": "portugal", "be": "belgien", "tr": "turkiet", "gr": "grekland",
    "ch": "schweiz", "at": "osterrike", "dk": "danmark", "no": "norge", "se": "sverige", "fi": "finland",
    "br": "brasilien", "ar": "argentina", "mx": "mexiko", "jp": "japan", "sa": "saudiarabien", "au": "australien",
    "holland": "nederlanderna", "nederland": "nederlanderna",
    "col": "colombia", "co": "colombia", "chi": "chile", "chl": "chile", "cl": "chile",
    "uru": "uruguay", "ury": "uruguay", "uy": "uruguay", "per": "peru", "pe": "peru",
    "ecu": "ecuador", "ec": "ecuador",
    "internationella": "internationell", "int": "internationell", "world": "internationell",
    "pol": "polen", "pl": "polen", "cze": "tjeckien", "cz": "tjeckien", "cro": "kroatien", "hrv": "kroatien",
    "srb": "serbien", "rou": "rumanien", "ro": "rumanien", "rus": "ryssland", "ukr": "ukraina", "isr": "israel",
    "cyp": "cypern", "chn": "kina", "kor": "sydkorea", "par": "paraguay", "pry": "paraguay", "ven": "venezuela",
    "bol": "bolivia", "crc": "costa rica", "cri": "costa rica", "egy": "egypten", "rsa": "sydafrika", "zaf": "sydafrika",
    "usa": "usa",
}

# --------------------------------------------------------------------------- #
# Viktad form. Tabellen delas i fjärdedelar: 1 = topplag ... 4 = bottenlag.
# Vinst mot topplag ger mest, förlust mot bottenlag kostar mest. Justera fritt.
# --------------------------------------------------------------------------- #
FORM_POINTS = {
    "V": {1: 4, 2: 3, 3: 2, 4: 1},
    "O": {1: 2, 2: 1, 3: 0, 4: -1},
    "F": {1: -1, 2: -2, 3: -3, 4: -4},
}
FORM_GAMES = 6  # antal senaste hemma- resp. bortamatcher som räknas (och visas på webbsidan)
FORM_ALL_GAMES = 8  # senaste matcherna oavsett plan - visas i kupongbyggaren med medelpoäng
ABSENT_GAMES = 10   # så många senaste ligamatcher räknas när spelarens betydelse (andel starter) räknas ut
KEY_PLAYER_PCT = 60 # startat i minst så här stor andel av matcherna = ordinarie
# Tidsviktning: senaste matchen räknas fullt, varje match längre bak räknas så här mycket
# av den efter (0.8 -> 1.0, 0.8, 0.64, 0.51, 0.41). 1.0 = ingen tidsviktning.
FORM_DECAY = 0.8
REST_FLAG_DAYS = 3  # flagga när lagen haft så här många dagars skillnad i vila

# --------------------------------------------------------------------------- #
# Flaggor - när är en match värd att läsa på om? Gränserna går att justera.
# --------------------------------------------------------------------------- #
FLAG_ODDS_MOVE = 3.0     # oddset har rört sig minst så många procentenheter sedan omgången öppnade
FLAG_STRECK = 8.0        # folket streckar minst så många procentenheter mer/mindre än oddsen
FLAG_FOLLOW = 4.0        # oddset har rört sig, men folket ligger minst så här långt efter åt andra hållet
FLAG_FORM = 1.5          # formskillnad (/m) som räknas som tydlig
FLAG_SHORT_REST = 3      # högst så många dagars vila = tätt schema

session = requests.Session()
# ESPN svarar 403 på okända klienter - vi skickar samma huvuden som en vanlig webbläsare.
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Origin": "https://www.espn.com",
    "Referer": "https://www.espn.com/",
})
# ESPN har flera värdnamn för samma API; om ett nekas provar vi nästa.
ESPN_HOSTS = ["site.api.espn.com", "site.web.api.espn.com"]


# --------------------------------------------------------------------------- #
# HTTP med diskcache (belastar inte källorna i onödan)
# --------------------------------------------------------------------------- #
def cached_get(url, params=None, ttl=6 * 3600):
    key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text("utf-8"))
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


def dump(name, data):
    RAW_DIR.mkdir(exist_ok=True)
    (RAW_DIR / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").replace("%", ""))
    except ValueError:
        return None


def num(v):
    """2.0 -> '2', None -> '-'"""
    f = to_float(v)
    if f is None:
        return "-"
    return str(int(f)) if f.is_integer() else f"{f:g}"


# --------------------------------------------------------------------------- #
# Svenska Spel
# --------------------------------------------------------------------------- #
def fetch_draws(product, dump_raw=False):
    errors = []
    for name in SVS_ENDPOINTS.get(product, [product]):
        try:
            data = cached_get(f"{SVS_BASE}/{name}/draws", ttl=300)
        except requests.RequestException as e:
            errors.append(f"{name}: {e.response.status_code if getattr(e, 'response', None) is not None else e}")
            continue
        if dump_raw:
            dump(f"svs_{name}.json", data)
        draws = data.get("draws") or ([data["draw"]] if data.get("draw") else [])
        if draws:
            # Första adressen som ger omgångar räcker - annars kan samma omgång komma två gånger
            return [parse_draw(d, name) for d in draws]
    if errors:
        raise requests.RequestException("inga omgångar hittades (" + ", ".join(errors) + ")")
    return []


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", ascii_lower(s)).strip("_") or "omgang"


def triple(obj):
    obj = obj or {}
    return {k: to_float(obj.get(k)) for k in ("one", "x", "two")}


def parse_draw(d, product):
    events = []
    for ev in d.get("drawEvents") or d.get("events") or []:
        m = ev.get("match") or {}
        parts = m.get("participants") or []
        home = next((p.get("name") for p in parts if p.get("type") == "home"), None)
        away = next((p.get("name") for p in parts if p.get("type") == "away"), None)
        if not (home and away):  # fallback: "Lag A - Lag B"
            desc = ev.get("eventDescription") or ev.get("description") or ""
            if " - " in desc:
                home, away = (s.strip() for s in desc.split(" - ", 1))
        events.append({
            "nr": ev.get("eventNumber"),
            "home": (home or "").strip(),
            "away": (away or "").strip(),
            "start": m.get("matchStart"),
            "league": (m.get("league") or {}).get("name"),
            "country": ((m.get("league") or {}).get("country") or {}).get("name"),
            "country_iso": ((m.get("league") or {}).get("country") or {}).get("isoCode"),
            "odds": triple(ev.get("odds")),
            "start_odds": triple(ev.get("startOdds")),
            "svenska_folket": triple(ev.get("svenskaFolket")),
        })
        # Svenska Spel tar bort aktuellt odds strax innan avspark - använd då startoddset i stället
        e = events[-1]
        if not any(v for v in (e["odds"] or {}).values()) and any(v for v in (e["start_odds"] or {}).values()):
            e["odds"] = dict(e["start_odds"])
            e["odds_from_start"] = True
    name = d.get("productName") or product.capitalize()
    return {
        # topptipsetfamily innehåller flera spel - productName skiljer dem åt ("Topptipset Europa" ...)
        "product": slug(name) if d.get("productName") else product,
        "product_name": name,
        "draw_number": d.get("drawNumber"),
        "comment": d.get("drawComment"),
        "close_time": d.get("regCloseTime") or d.get("closeTime"),
        "events": events,
        "fund": {"rollover": _kr((d.get("fund") or {}).get("rolloverIn")),
                 "extra": _kr((d.get("fund") or {}).get("extraMoney"))},
    }


def _kr(v):
    """'1 234 567,00' -> 1234567.0 (Svenska Spels beloppsformat)."""
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# Namnmatchning (Svenska Spel skriver svenska/förkortade namn)
# --------------------------------------------------------------------------- #
def ascii_lower(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


STOPWORDS = {"fc", "afc", "cf", "sc", "ac", "ssc", "as", "if", "ff", "bk", "sk", "the"}
ALIASES = {
    "manchester u": "manchester united", "man utd": "manchester united",
    "manchester c": "manchester city", "man city": "manchester city",
    "bayern munchen": "bayern munich", "qpr": "queens park rangers",
    "psg": "paris saint germain", "m gladbach": "monchengladbach",
    "gladbach": "monchengladbach",
    # Landslag (ESPN använder engelska namn). Korta former matchar flera stavningar,
    # t.ex. "czech" träffar både "Czechia" och "Czech Republic".
    "sverige": "sweden", "norge": "norway", "danmark": "denmark", "island": "iceland",
    "faroarna": "faroe", "tyskland": "germany", "nederlanderna": "netherlands",
    "belgien": "belgium", "frankrike": "france", "spanien": "spain", "italien": "italy",
    "skottland": "scotland", "nordirland": "northern ireland", "irland": "ireland",
    "schweiz": "switzerland", "osterrike": "austria", "polen": "poland",
    "tjeckien": "czech", "slovakien": "slovakia", "ungern": "hungary",
    "rumanien": "romania", "bulgarien": "bulgaria", "grekland": "greece", "turkiet": "turk",
    "kroatien": "croatia", "serbien": "serbia", "slovenien": "slovenia",
    "bosnien hercegovina": "bosnia", "albanien": "albania",
    "nordmakedonien": "north macedonia", "ukraina": "ukraine", "ryssland": "russia",
    "vitryssland": "belarus", "litauen": "lithuania", "lettland": "latvia",
    "estland": "estonia", "georgien": "georgia", "armenien": "armenia",
    "azerbajdzjan": "azerbaijan", "kazakstan": "kazakhstan", "cypern": "cyprus",
    "luxemburg": "luxembourg", "moldavien": "moldova",
}


def norm(name):
    n = " ".join(w for w in ascii_lower(name).split() if w not in STOPWORDS)
    return ALIASES.get(n, n)


def similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def _lookup(n, table):
    if n in table:
        return table[n]
    for key in sorted(table, key=len, reverse=True):  # längsta först: "2 bundesliga" före "bundesliga"
        if key in n:
            return table[key]
    return None


def league_slug(name, country=None, country_iso=None):
    """Liganamn + land (Svenska Spels) -> ESPN:s liga-id. Landet avgör t.ex. Serie A i Italien eller Brasilien."""
    n = ascii_lower(name)
    for c in (ascii_lower(country_iso), ascii_lower(country)):
        table = LEAGUES_BY_COUNTRY.get(COUNTRY_ALIASES.get(c, c))
        if table:
            hit = _lookup(n, table)
            if hit:
                return hit
    return _lookup(n, LEAGUES)


# --------------------------------------------------------------------------- #
# ESPN-tolkning (defensiv - API:t är odokumenterat)
# --------------------------------------------------------------------------- #
def find_entries(obj):
    """Letar rekursivt upp tabellrader ('entries') var de än ligger i svaret."""
    if isinstance(obj, dict):
        entries = obj.get("entries")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict) and "team" in entries[0]:
            for e in entries:
                yield e, len(entries)   # gruppens storlek - Nations League har grupper om 4
        for v in obj.values():
            yield from find_entries(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_entries(v)


def parse_table(data):
    rows = {}
    for e, group_size in find_entries(data or {}):
        team = e.get("team")
        tid = str(team.get("id")) if isinstance(team, dict) else str(e.get("id") or "")
        rows[tid] = {s.get("name") or s.get("type"): s.get("value")
                     for s in e.get("stats", []) if isinstance(s, dict)}
        rows[tid]["group_size"] = group_size
    return rows


def parse_injuries(data):
    out = {}
    for t in (data or {}).get("injuries") or []:
        tid = str(t.get("id") or (t.get("team") or {}).get("id") or "")
        players = []
        for inj in t.get("injuries") or []:
            det = inj.get("details") or {}
            typ = inj.get("type")
            players.append({
                "player": (inj.get("athlete") or {}).get("displayName"),
                "status": inj.get("status") or (typ.get("description") if isinstance(typ, dict) else typ),
                "reason": det.get("type") or det.get("detail") or inj.get("shortComment"),
                "return": det.get("returnDate"),
            })
        if tid and players:
            out[tid] = players
    return out


def parse_absent(roster, league_injuries):
    """Skadade/avstängda/osäkra spelare ur lagets trupp hos ESPN (+ ligans skadelista)."""
    out = {}
    for a in (roster or {}).get("athletes") or []:
        items = a.get("items") if isinstance(a.get("items"), list) else [a]   # ibland grupperat per position
        for p in items:
            st = (p.get("status") or {})
            inj = (p.get("injuries") or [])
            stype = (st.get("type") or st.get("name") or "").lower()
            if not inj and stype in ("", "active"):
                continue
            i0 = inj[0] if inj else {}
            det = i0.get("details") or {}
            typ = i0.get("type")
            out[str(p.get("id"))] = {
                "id": str(p.get("id")), "name": p.get("displayName") or p.get("fullName"),
                "pos": (p.get("position") or {}).get("abbreviation") or (p.get("position") or {}).get("name"),
                "status": i0.get("status") or st.get("name") or "Borta",
                "reason": det.get("type") or det.get("detail") or (typ.get("description") if isinstance(typ, dict) else typ)
                or i0.get("shortComment"),
                "return": det.get("returnDate"),
            }
    names = {v["name"] for v in out.values()}
    for p in league_injuries or []:
        if p.get("player") and p["player"] not in names:
            out["n:" + p["player"]] = {"id": None, "name": p["player"], "pos": None, "status": p.get("status"),
                                       "reason": p.get("reason"), "return": p.get("return")}
    return list(out.values())


def player_usage(summaries, tid):
    """{spelar-id: {"starts", "apps", "name"}} och antal matcher, ur matchernas startelvor hos ESPN."""
    use, games = {}, 0
    for sm in summaries:
        for r in (sm or {}).get("rosters") or []:
            if str((r.get("team") or {}).get("id")) != tid:
                continue
            games += 1
            for p in r.get("roster") or []:
                aid = str((p.get("athlete") or {}).get("id"))
                u = use.setdefault(aid, {"starts": 0, "apps": 0, "name": (p.get("athlete") or {}).get("displayName")})
                if p.get("starter"):
                    u["starts"] += 1
                    u["apps"] += 1
                elif p.get("subbedIn"):
                    u["apps"] += 1
    return use, games


def parse_last5(summary):
    """Senaste matcherna per lag: {team_id: ["2026-09-01 @ Bradford 2-1 (V)", ...]}"""
    s = summary or {}
    blocks = s.get("lastFiveGames") or (s.get("boxscore") or {}).get("form") or []
    sv = {"W": "V", "D": "O", "L": "F"}
    out = {}
    for b in blocks:
        tid = str((b.get("team") or {}).get("id"))
        games = []
        for e in (b.get("events") or [])[:5]:
            opp = (e.get("opponent") or {}).get("displayName", "?")
            home = str(e.get("homeTeamId")) == tid if e.get("homeTeamId") else e.get("atVs") != "@"
            # ESPN:s "score" skrivs med vinnarens mål först - vi räknar om till lagets perspektiv.
            hs, as_ = to_float(e.get("homeTeamScore")), to_float(e.get("awayTeamScore"))
            if hs is not None and as_ is not None:
                own, other = (hs, as_) if home else (as_, hs)
                score = f"{num(own)}-{num(other)}"
            else:
                score = e.get("score", "")
            if to_float(e.get("homeShootoutScore")) or to_float(e.get("awayShootoutScore")):
                score += " str"
            res = sv.get(e.get("gameResult", ""), e.get("gameResult", "?"))
            comp = e.get("leagueAbbreviation") or e.get("leagueName") or ""
            games.append({"date": (e.get("gameDate") or "")[:10], "home": home, "opponent": opp,
                          "score": score, "result": res, "competition": comp})
        out[tid] = games
    return out


def quartile(rank, n_teams):
    """Tabellplats -> fjärdedel 1-4 (24 lag: plats 1-6 = 1, 7-12 = 2, 13-18 = 3, 19-24 = 4)."""
    r = to_float(rank)
    if r is None or not n_teams:
        return None
    return min(4, int((r - 1) // (n_teams / 4)) + 1)


def score_value(s):
    if isinstance(s, dict):
        return to_float(s.get("value") if s.get("value") is not None else s.get("displayValue"))
    return to_float(s)


SEASON_GAP_DAYS = 50   # längre uppehåll än så mellan två ligamatcher = ny säsong (sommaruppehåll)


def parse_schedule(data, tid):
    """
    Lagets spelade ligamatcher den här säsongen, senaste först.
    Bara ligans eget schema hos ESPN (inga cuper, europaspel eller träningsmatcher). Matcher från förra
    säsongen tas bort: ESPN:s säsongsår om det finns, annars allt före det senaste långa uppehållet.
    """
    games = []
    season_year = ((data or {}).get("season") or {}).get("year") or ((data or {}).get("requestedSeason") or {}).get("year")
    for ev in (data or {}).get("events") or []:
        ev_year = (ev.get("season") or {}).get("year")
        if season_year and ev_year and ev_year != season_year:
            continue
        stype = ((ev.get("seasonType") or {}).get("name") or "").lower()
        if any(w in stype for w in ("friendl", "pre-season", "preseason")):
            continue
        comp = (ev.get("competitions") or [{}])[0]
        st = ((comp.get("status") or ev.get("status") or {}).get("type") or {})
        if not st.get("completed"):
            continue
        cs = comp.get("competitors") or []
        me = next((c for c in cs if str((c.get("team") or {}).get("id")) == tid), None)
        opp = next((c for c in cs if c is not me), None)
        if not me or not opp:
            continue
        gf, ga = score_value(me.get("score")), score_value(opp.get("score"))
        if gf is None or ga is None:
            continue
        games.append({
            "event_id": ev.get("id"),
            "date": (ev.get("date") or "")[:10],
            "home": me.get("homeAway") == "home",
            "opp_id": str((opp.get("team") or {}).get("id")),
            "opp_name": (opp.get("team") or {}).get("shortDisplayName") or (opp.get("team") or {}).get("displayName"),
            "gf": int(gf), "ga": int(ga),
            "res": "V" if gf > ga else "O" if gf == ga else "F",
        })
    games.sort(key=lambda g: g["date"], reverse=True)
    for i in range(1, len(games)):
        try:
            gap = (datetime.fromisoformat(games[i - 1]["date"]) - datetime.fromisoformat(games[i]["date"])).days
        except ValueError:
            continue
        if gap > SEASON_GAP_DAYS:
            return games[:i]
    return games


def form_grade(x):
    """Formbetyg 0-10 av viktad poäng per match (-4..+4). 5 = medel, 10 = vunnit allt mot topplag."""
    return None if x is None else max(0.0, min(10.0, (x + 4) * 1.25))


def grade_s(x):
    g = form_grade(x)
    return "-" if g is None else f"{g:.1f}".replace(".", ",")


def grade_word(x):
    g = form_grade(x)
    if g is None:
        return ""
    return ("toppform" if g >= 7.5 else "bra form" if g >= 6 else "medelform" if g >= 4 else
            "svag form" if g >= 2.5 else "dålig form")


def weighted_form(games, table, n=None):
    """Viktar varje match efter motståndarens fjärdedel i tabellen (eller gruppen)."""
    out, total, tw_sum, w_sum = [], 0, 0.0, 0.0
    weighted = bool(table)
    for i, g in enumerate(games[:n or FORM_GAMES]):   # games är sorterade med senaste först
        row = (table or {}).get(g["opp_id"]) or {}
        rank = row.get("rank")
        q = quartile(rank, row.get("group_size"))
        pts = FORM_POINTS[g["res"]].get(q) if q else None
        time_w = FORM_DECAY ** i
        if pts is None:
            weighted = False
        else:
            total += pts
            tw_sum += time_w * pts
            w_sum += time_w
        out.append({**g, "opp_rank": rank, "opp_quartile": q, "points": pts, "time_weight": round(time_w, 2)})
    ok = weighted and out and w_sum
    return {
        "games": out,
        "form": "".join(g["res"] for g in out),
        "weighted": total if ok else None,                 # summa poäng, utan tidsviktning
        "per_game": round(tw_sum / w_sum, 2) if ok else None,  # tidsviktat snitt per match
        "mean": round(total / len(out), 2) if ok else None,    # medelpoäng (utan tidsviktning)
        "ppg": round(sum(3 if g["res"] == "V" else 1 if g["res"] == "O" else 0 for g in out) / len(out), 2)
        if out else None,                                     # vanliga poäng per match (3/1/0)
    }


def is_domestic(comp):
    """Inhemsk liga/cup (inte Europacup, landskamp eller träningsmatch)."""
    c = ascii_lower(comp or "")
    return bool(c) and not any(w in c for w in ("uefa", "friendly", "champions league", "europa", "conference",
                                                "world cup", "nations", "qualif", "copa libertadores", "sudamericana"))


LEAGUE_SHORT = {
    "esp.1": "LaLiga", "ita.1": "Serie A", "eng.1": "Premier League", "ger.1": "Bundesliga", "fra.1": "Ligue 1",
    "ned.1": "Eredivisie", "por.1": "Liga Portugal", "tur.1": "Süper Lig", "gre.1": "grekiska ligan",
    "den.1": "danska ligan", "sco.1": "skotska ligan", "bel.1": "belgiska ligan", "aut.1": "österrikiska ligan",
    "sui.1": "schweiziska ligan", "nor.1": "Eliteserien", "swe.1": "Allsvenskan", "pol.1": "Ekstraklasa",
    "rus.1": "ryska ligan", "ukr.1": "ukrainska ligan", "cyp.1": "cypriotiska ligan", "isr.1": "israeliska ligan",
    "irl.1": "irländska ligan", "fin.1": "Veikkausliiga",
}
DOMESTIC_COMP = {  # ESPN:s tävlingsnamn i "senaste matcher" -> ESPN-liga (för Europacupmatcher)
    "laliga": "esp.1", "la liga": "esp.1", "spanish laliga": "esp.1", "serie a": "ita.1", "italian serie a": "ita.1",
    "ligue 1": "fra.1", "french ligue 1": "fra.1", "bundesliga": "ger.1", "german bundesliga": "ger.1",
    "premier league": "eng.1", "english premier league": "eng.1", "eredivisie": "ned.1", "dutch eredivisie": "ned.1",
    "portuguese liga": "por.1", "liga portugal": "por.1", "primeira liga": "por.1", "belgian pro league": "bel.1",
    "jupiler pro league": "bel.1", "turkish super lig": "tur.1", "super lig": "tur.1", "greek super league": "gre.1",
    "danish superliga": "den.1", "superliga": "den.1", "spfl premiership": "sco.1", "scottish premiership": "sco.1",
    "austrian bundesliga": "aut.1", "swiss super league": "sui.1", "eliteserien": "nor.1", "norwegian eliteserien": "nor.1",
    "allsvenskan": "swe.1", "swedish allsvenskan": "swe.1", "russian premier league": "rus.1", "ekstraklasa": "pol.1",
    "polish ekstraklasa": "pol.1", "cypriot first division": "cyp.1", "israeli premier league": "isr.1",
    "ukrainian premier league": "ukr.1", "irish premier division": "irl.1", "veikkausliiga": "fin.1",
}
CUP_WORDS = ("cup", "pokal", "coppa", "copa", "taca", "coupe", "trophy", "slc", "dfb", "beker", "shield", "super cup",
             "supercopa", "supercoppa")


def domestic_slug(last_games):
    """Lagets inhemska liga utifrån dess senaste matcher (vanligast förekommande ligan, inte cuper)."""
    count = {}
    for g in last_games or []:
        comp = ascii_lower(g.get("competition") or "")
        if not is_domestic(comp) or any(w in comp for w in CUP_WORDS):
            continue
        sl = DOMESTIC_COMP.get(comp) or league_slug(comp)
        if sl and not sl.startswith("uefa"):
            count[sl] = count.get(sl, 0) + 1
    return max(count, key=count.get) if count else None


def checked_rest(rest, last_games):
    """
    ESPN saknar flera ligor (t.ex. Kroatien, Ungern, Tjeckien, Bulgarien). Då syns bara Europacupmatcherna och
    vilan ser ut att vara flera veckor fast laget spelade ligamatch i helgen. Saknas inhemska matcher helt
    i lagets senaste matcher och senaste kända match är mer än en vecka gammal räknas vilan som okänd.
    """
    if rest and rest["days"] > 7 and last_games and not any(is_domestic(g.get("competition")) for g in last_games):
        return None
    return rest


def rest_days(match_date, *game_lists):
    """Dagar sedan lagets senaste match före match_date, i alla tävlingar."""
    last = None
    for games in game_lists:
        for g in games or []:
            d = g.get("date")
            if d and d < match_date and (last is None or d > last["date"]):
                last = g
    if not last:
        return None
    days = (datetime.fromisoformat(match_date) - datetime.fromisoformat(last["date"])).days
    return {"days": days, "date": last["date"],
            "opponent": last.get("opponent") or last.get("opp_name"),
            "competition": last.get("competition") or "liga"}


def record(c):
    """Vinst-oavgjort-förlust från ESPN:s matchlista, t.ex. '5-0-2'."""
    for r in c.get("records") or []:
        if r.get("type") == "total" and r.get("summary"):
            try:
                w, d, l = (int(x) for x in r["summary"].split("-"))
                return {"w": w, "d": d, "l": l, "played": w + d + l, "points": 3 * w + d}
            except ValueError:
                pass
    return None


def parse_h2h(summary, n=5):
    for block in (summary or {}).get("headToHeadGames") or []:
        team = (block.get("team") or {}).get("displayName", "")
        games = []
        for e in (block.get("events") or [])[:n]:
            opp = (e.get("opponent") or {}).get("displayName", "?")
            games.append(f'{(e.get("gameDate") or "")[:10]} {team} {e.get("atVs", "vs")} '
                         f'{opp} {e.get("score", "")} ({e.get("gameResult", "")})')
        return games  # båda lagens block innehåller samma matcher - första räcker
    return []


class Espn:
    def __init__(self, dump_raw=False, no_absent=False):
        self.dump_raw = dump_raw
        self.no_absent = no_absent
        self.shown = set()
        self.league_names = {}

    def _get(self, url, ttl, **params):
        last = None
        for host in ESPN_HOSTS:
            try:
                return cached_get(url.replace(ESPN_HOSTS[0], host, 1), params or None, ttl)
            except (requests.RequestException, ValueError) as e:
                last = e
        msg = f"  ESPN: {url} -> {last}"
        if msg not in self.shown:
            self.shown.add(msg)
            print(msg, file=sys.stderr)
        return {}

    def find_event(self, slug, date, home, away):
        sb = self._get(f"{ESPN_SITE}/{slug}/scoreboard", 3600, dates=date.replace("-", ""))

        def score(name, team):
            names = [team.get(k) for k in ("displayName", "shortDisplayName", "name", "location") if team.get(k)]
            return max((similarity(name, n) for n in names), default=0.0)

        lg = (sb.get("leagues") or [{}])[0]
        self.league_names[slug] = " ".join(x for x in (lg.get("abbreviation"), lg.get("name")) if x)
        best, best_score = None, 0.0
        for ev in sb.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            cs = {c.get("homeAway"): c for c in comp.get("competitors") or []}
            if "home" in cs and "away" in cs:
                s = (score(home, cs["home"].get("team") or {}) + score(away, cs["away"].get("team") or {})) / 2
                if s > best_score:
                    best, best_score = (ev, cs["home"], cs["away"]), s
        return best if best_score >= 0.65 else None

    def enrich(self, event):
        if not (event["home"] and event["away"] and event["start"]):
            return None
        slug = league_slug(event["league"], event.get("country"), event.get("country_iso"))
        if not slug:
            land = f' ({event["country"]})' if event.get("country") else ""
            return {"error": f'Okänd liga "{event["league"]}"{land} - lägg till den i LEAGUES_BY_COUNTRY'}
        date = event["start"][:10]
        found = self.find_event(slug, date, event["home"], event["away"])
        if not found:
            # ESPN sorterar matcher på amerikansk tid. Kvällsmatcher i Sydamerika/USA (efter midnatt svensk
            # tid) ligger därför ofta på dagen innan hos ESPN - och ibland tvärtom. Prova dagarna runt.
            from datetime import date as _date, timedelta as _td
            d0 = _date.fromisoformat(date)
            for delta in (-1, 1):
                found = self.find_event(slug, (d0 + _td(days=delta)).isoformat(), event["home"], event["away"])
                if found:
                    break
        if not found:
            return {"error": f"Hittade inte matchen hos ESPN ({slug}, {date} ±1 dag)"}
        ev, home_c, away_c = found

        summary = self._get(f"{ESPN_SITE}/{slug}/summary", 3600, event=ev["id"])
        if self.dump_raw:
            dump(f'espn_{slug}_{ev["id"]}.json', summary)

        table = parse_table(self._get(f"{ESPN_STANDINGS}/{slug}/standings", 6 * 3600)) \
            or parse_table(summary.get("standings"))
        injuries = parse_injuries(self._get(f"{ESPN_SITE}/{slug}/injuries", 3 * 3600))
        injuries.update(parse_injuries(summary))
        last5 = parse_last5(summary)

        # ESPN:s tabell-endpoint kan ligga efter (visade 1 spelad match i september).
        # Tabellen används bara om antalet spelade matcher stämmer med matchlistans facit.
        for c in (home_c, away_c):
            rec = record(c)
            row = table.get(str((c.get("team") or {}).get("id")), {})
            if rec and rec["played"] and to_float(row.get("gamesPlayed")) != rec["played"]:
                table = {}

        cup = slug.startswith("uefa.") or slug in ("conmebol.libertadores", "conmebol.sudamericana", "concacaf.champions")

        def team_info(c):
            t = c.get("team") or {}
            tid = str(t.get("id"))
            rec = record(c)
            row = table.get(tid, {})
            tab, lslug, cup_rank = table, slug, None
            sched = parse_schedule(self._get(f"{ESPN_SITE}/{slug}/teams/{tid}/schedule", 6 * 3600), tid)
            cup_sched = sched
            if cup:
                # Europacup: formen och tabellen ska komma från lagets egen liga, inte från cupens få matcher
                dslug = domestic_slug(last5.get(tid, []))
                if dslug:
                    try:
                        dtab = parse_table(self._get(f"{ESPN_STANDINGS}/{dslug}/standings", 6 * 3600))
                        dsched = parse_schedule(self._get(f"{ESPN_SITE}/{dslug}/teams/{tid}/schedule", 6 * 3600), tid)
                    except (requests.RequestException, ValueError, KeyError, TypeError):
                        dtab, dsched = {}, []
                    if dsched:
                        cup_rank = row.get("rank")
                        tab, lslug, sched = dtab, dslug, dsched
                        row = dtab.get(tid, {})
                        w = sum(g["res"] == "V" for g in dsched)
                        d = sum(g["res"] == "O" for g in dsched)
                        rec = {"w": w, "d": d, "l": len(dsched) - w - d, "played": len(dsched), "points": 3 * w + d}
            absent = []
            if not self.no_absent:
                roster = self._get(f"{ESPN_SITE}/{slug}/teams/{tid}/roster", 6 * 3600)
                absent = parse_absent(roster, injuries.get(tid, []))
                if absent:
                    # Spelade matcher ändras inte - sparas länge
                    sums = [self._get(f"{ESPN_SITE}/{slug}/summary", 30 * 86400, event=g["event_id"])
                            for g in sched[:ABSENT_GAMES] if g.get("event_id")]
                    use, n_games = player_usage(sums, tid)
                    by_name = {u["name"]: u for u in use.values()}
                    for p in absent:
                        u = use.get(p["id"]) or by_name.get(p["name"]) or {}
                        p.update(starts=u.get("starts", 0), apps=u.get("apps", 0), games=n_games,
                                 start_pct=round(100 * u.get("starts", 0) / n_games) if n_games else None)
                    absent.sort(key=lambda p: -(p.get("start_pct") or 0))
            return {
                "absent": absent,
                "form_home": weighted_form([g for g in sched if g["home"]], tab),
                "form_away": weighted_form([g for g in sched if not g["home"]], tab),
                "form_all": weighted_form(sched, tab, FORM_ALL_GAMES),
                "form_league": lslug,
                "cup_rank": cup_rank,
                "quartile": quartile(row.get("rank"), row.get("group_size")),
                "group_size": row.get("group_size"),
                "id": tid,
                "name": t.get("displayName"),
                "rank": row.get("rank"),
                "record": rec,
                "goal_diff": row.get("pointDifferential"),
                "form": c.get("form") or "",
                "last5": last5.get(tid, []),
                # Ligaschemat har bara ligamatcher; lastFiveGames har även cupmatcher.
                "rest": checked_rest(rest_days(event["start"][:10], sched, cup_sched, last5.get(tid, [])),
                                     last5.get(tid, []) + ([{"competition": lslug}] if lslug != slug else [])),
                "injuries": injuries.get(tid, []),
            }

        status = ((ev.get("status") or {}).get("type") or {})
        hi, ai = team_info(home_c), team_info(away_c)
        return {
            "espn_event_id": ev["id"],
            "cross_league": hi.get("form_league") != ai.get("form_league"),
            "espn_league": slug,
            "league_name": self.league_names.get(slug, ""),
            "status": status.get("description") if status.get("name") != "STATUS_SCHEDULED" else None,
            "home": hi,
            "away": ai,
            "h2h": parse_h2h(summary),
        }


# --------------------------------------------------------------------------- #
# xG och expected points från football-data.co.uk (HxG/AxG finns från säsong 2026/27)
# --------------------------------------------------------------------------- #
FD_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
FD_LEAGUE = {  # ESPN-liga -> football-datas ligakod
    "eng.1": "E0", "eng.2": "E1", "eng.3": "E2", "eng.4": "E3", "eng.5": "EC",
    "sco.1": "SC0", "sco.2": "SC1", "sco.3": "SC2", "sco.4": "SC3",
    "ger.1": "D1", "ger.2": "D2", "ita.1": "I1", "ita.2": "I2", "esp.1": "SP1", "esp.2": "SP2",
    "fra.1": "F1", "fra.2": "F2", "ned.1": "N1", "bel.1": "B1", "por.1": "P1", "tur.1": "T1", "gre.1": "G1",
}
FD_TEAM_ALIASES = {  # football-datas förkortningar -> fullare namn
    "man united": "manchester united", "man city": "manchester city", "nott m forest": "nottingham forest",
    "sheffield weds": "sheffield wednesday", "wolves": "wolverhampton wanderers", "bristol rvs": "bristol rovers",
    "peterboro": "peterborough united", "west brom": "west bromwich albion", "qpr": "queens park rangers",
}
XG_SHRINK = 3        # antal "låtsasmatcher" på ligasnittet - dämpar små urval tidigt på säsongen
XG_RECENT = 6        # senaste N matcher för trend
FLAG_XG = 10.0       # xG-modellen och oddsen skiljer minst så många procentenheter (modellen är enklare än oddsen)
FLAG_LUCK = 0.5      # poäng per match över/under xPts som räknas som tur/otur
FLAG_LUCK_MIN = 5    # ... men bara med minst så många matcher
FLAG_XTABLE = 6      # skillnad i placering mellan tabell och xG-tabell som flaggas

# --------------------------------------------------------------------------- #
# Prognos och kupongförslag
# --------------------------------------------------------------------------- #
# Prognos = vägt snitt av oddsen och två statistikmodeller. Saknas en modell tar den andra dess vikt,
# saknas båda används bara oddsen.
PROGNOS_ODDS_WEIGHT = 1.0   # oddsen (vikt_test.py, 10 000 matcher: modellerna gör prognosen sämre)
PROGNOS_XPTS_WEIGHT = 0.0   # xPts-modellen: lagens förväntade vinst/oavgjort/förlust match för match
PROGNOS_XG_WEIGHT = 0.0     # xG-modellen: snitt-xG framåt/bakåt -> Poisson
# Modellerna (Poisson) ger krysset för låg chans. True = krysset tas från oddsen och modellerna får bara
# flytta chansen mellan 1:an och 2:an. Då blir det inte färre kryss för att modellerna väger in.
PROGNOS_KRYSS_FRAN_ODDS = True
# Formjustering: oddsen flyttas lite mot laget med bäst viktad form (medelpoäng, 8 senaste ligamatcherna).
# Testat på 2 813 engelska matcher, tränat på en säsong och testat på den andra: liten men stabil förbättring.
# FORM_ADJ_K = procentenheter per poängs skillnad i medelpoäng. 0 = av. Krysset rörs inte.
FORM_ADJ_K = 1.0
FORM_ADJ_MAX = 5.0      # högst så många procentenheter åt något håll
FORM_ADJ_MIN_GAMES = 4  # båda lagen måste ha minst så många matcher den här säsongen
SPIK_MIN = 55.0             # spika om mest troliga tecknet har minst så här hög sannolikhet (%)
HALV_MIN = 70.0             # halvgardera om de två troligaste tillsammans når minst så här (%)
VALUE_MIN_RATIO = 1.25      # spelvärde: vår sannolikhet minst 25 % högre än folkets streck ...
VALUE_MIN_PROB = 22.0       # ... och tecknet måste ha minst så här stor chans (%) för att räknas
RADER = 96                  # radbudget för kupongförslaget (ändras med --rader)
TIPS_MIN = 40.0             # "Tips"-kolumnen: ett tecken om det har minst så här stor chans (%) ...
TIPS_LEAD = 5.0             # ... och leder med minst så många procentenheter, annars 1X2


def current_fd_season(today=None):
    from datetime import date
    today = today or date.today()
    y = today.year % 100
    return f"{y:02d}{y + 1:02d}" if today.month >= 7 else f"{y - 1:02d}{y:02d}"


def poisson_1x2(lh, la, max_goals=10):
    """Sannolikhet för 1/X/2 om målen är Poisson-fördelade med snitten lh och la."""
    import math
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(max_goals + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(max_goals + 1)]
    one = sum(ph[i] * pa[j] for i in range(len(ph)) for j in range(len(pa)) if i > j)
    x = sum(ph[i] * pa[i] for i in range(len(ph)))
    return one, x, max(0.0, 1 - one - x)


def poisson_extras(lh, la, max_goals=10):
    """Över 2,5 mål, båda lagen gör mål, 0-0 och de troligaste resultaten."""
    import math
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(max_goals + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(max_goals + 1)]
    grid = [(i, j, ph[i] * pa[j]) for i in range(max_goals + 1) for j in range(max_goals + 1)]
    over25 = sum(p for i, j, p in grid if i + j >= 3)
    btts = (1 - ph[0]) * (1 - pa[0])
    top = sorted(grid, key=lambda x: -x[2])[:3]
    return {"over25": over25, "btts": btts, "nil_nil": ph[0] * pa[0],
            "cs_home": pa[0], "cs_away": ph[0],
            "top_scores": [(f"{i}-{j}", p) for i, j, p in top]}


class XGData:
    def __init__(self, season=None):
        self.season = season or current_fd_season()
        self.leagues = {}
        self.shown = set()

    def _csv(self, code):
        CACHE_DIR.mkdir(exist_ok=True)
        path = CACHE_DIR / f"fd_{self.season}_{code}.csv"
        if not path.exists() or time.time() - path.stat().st_mtime > 12 * 3600:
            r = session.get(FD_URL.format(season=self.season, code=code), timeout=30)
            r.raise_for_status()
            path.write_bytes(r.content)
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        import csv
        import io
        return list(csv.DictReader(io.StringIO(text)))

    def league(self, slug):
        """{lag: {alla, hemma, borta, senaste}} + ligasnitt, eller None om ligan saknar xG."""
        if slug in self.leagues:
            return self.leagues[slug]
        code = FD_LEAGUE.get(slug)
        data = None
        if code:
            try:
                data = self._build(self._csv(code))
            except (requests.RequestException, ValueError, KeyError) as e:
                msg = f"  xG: kunde inte hämta {code} ({self.season}): {e}"
                if msg not in self.shown:
                    self.shown.add(msg)
                    print(msg, file=sys.stderr)
        self.leagues[slug] = data
        return data

    @staticmethod
    def _build(rows):
        matches = []
        for r in rows:
            try:
                hxg, axg = float(r["HxG"]), float(r["AxG"])
                hg, ag = int(float(r["FTHG"])), int(float(r["FTAG"]))
            except (KeyError, TypeError, ValueError):
                continue
            d = r.get("Date", "")
            try:
                dt = datetime.strptime(d, "%d/%m/%Y")
            except ValueError:
                try:
                    dt = datetime.strptime(d, "%d/%m/%y")
                except ValueError:
                    continue
            def opt(k):
                try:
                    return float(r.get(k))
                except (TypeError, ValueError):
                    return None
            matches.append({"date": dt.strftime("%Y-%m-%d"), "home": r["HomeTeam"], "away": r["AwayTeam"],
                            "hg": hg, "ag": ag, "hxg": hxg, "axg": axg,
                            "hs": opt("HS"), "as": opt("AS"), "hst": opt("HST"), "ast": opt("AST"),
                            "hc": opt("HC"), "ac": opt("AC")})
        if not matches:
            return None
        matches.sort(key=lambda m: m["date"])
        teams = {}
        for m in matches:
            p1, px, p2 = poisson_1x2(m["hxg"], m["axg"])
            ex = poisson_extras(m["hxg"], m["axg"])
            for side, team in (("home", m["home"]), ("away", m["away"])):
                h = side == "home"
                own_xg, opp_xg = (m["hxg"], m["axg"]) if h else (m["axg"], m["hxg"])
                gf, ga = (m["hg"], m["ag"]) if h else (m["ag"], m["hg"])
                pw, pl = (p1, p2) if h else (p2, p1)
                pts = 3 if gf > ga else 1 if gf == ga else 0
                teams.setdefault(team, []).append({
                    "date": m["date"], "home": h, "opp": m["away"] if h else m["home"],
                    "xg": own_xg, "xga": opp_xg, "gf": gf, "ga": ga,
                    "xpts": 3 * pw + px, "pts": pts,
                    # förväntat utfall enligt chanserna (summeras till förväntade V-O-F)
                    "xw": pw, "xd": px, "xl": pl,
                    "w": gf > ga, "d": gf == ga, "l": gf < ga,
                    "xcs": ex["cs_home"] if h else ex["cs_away"], "cs": ga == 0,
                    "xbtts": ex["btts"], "btts": gf > 0 and ga > 0,
                    "xo25": ex["over25"], "o25": gf + ga >= 3,
                    "shots": m["hs"] if h else m["as"], "shots_a": m["as"] if h else m["hs"],
                    "sot": m["hst"] if h else m["ast"], "sot_a": m["ast"] if h else m["hst"],
                    "corners": m["hc"] if h else m["ac"], "corners_a": m["ac"] if h else m["hc"],
                })
        n = len(matches)
        lg = {"teams": teams,
              "avg_home_xg": sum(m["hxg"] for m in matches) / n,
              "avg_away_xg": sum(m["axg"] for m in matches) / n,
              "n_matches": n}
        hw = [poisson_1x2(m["hxg"], m["axg"]) for m in matches]
        lg["avg_home_xwdl"] = tuple(sum(x[i] for x in hw) / n for i in range(3))   # hemmalagets xV, xO, xF
        lg["tables"] = XGData._tables(teams)
        return lg

    @staticmethod
    def _tables(teams):
        """Placering i vanlig tabell och i xG-tabellen (xPts), totalt/hemma/borta."""
        out = {t: {} for t in teams}
        for key, flt in (("all", lambda g: True), ("home", lambda g: g["home"]), ("away", lambda g: not g["home"])):
            rows = []
            for t, games in teams.items():
                gs = [g for g in games if flt(g)]
                rows.append((t, sum(g["pts"] for g in gs), sum(g["gf"] - g["ga"] for g in gs),
                             sum(g["xpts"] for g in gs), sum(g["xg"] - g["xga"] for g in gs)))
            for i, (t, *_rest) in enumerate(sorted(rows, key=lambda r: (-r[1], -r[2], r[0]))):
                out[t][f"rank_{key}"] = i + 1
            for i, (t, *_rest) in enumerate(sorted(rows, key=lambda r: (-r[3], -r[4], r[0]))):
                out[t][f"xrank_{key}"] = i + 1
        for t in out:
            out[t]["n_teams"] = len(teams)
        return out

    @staticmethod
    def summarize(games):
        if not games:
            return None
        n = len(games)
        s = lambda k: sum(g[k] for g in games)

        def per(k):   # snitt per match för kolumner som kan saknas (skott, hörnor)
            vals = [g[k] for g in games if g.get(k) is not None]
            return sum(vals) / len(vals) if vals else None

        shots, sot = per("shots"), per("sot")
        shots_a = per("shots_a")
        out = {"n": n, "xg": s("xg") / n, "xga": s("xga") / n, "gf": s("gf") / n, "ga": s("ga") / n,
               "xpts": s("xpts") / n, "pts": s("pts") / n,
               "xg_tot": s("xg"), "xga_tot": s("xga"), "gf_tot": s("gf"), "ga_tot": s("ga"),
               "xpts_tot": s("xpts"), "pts_tot": s("pts"),
               "w": s("w"), "d": s("d"), "l": s("l"), "xw": s("xw"), "xd": s("xd"), "xl": s("xl"),
               "cs": s("cs"), "xcs": s("xcs"),
               "btts": s("btts") / n, "xbtts": s("xbtts") / n, "o25": s("o25") / n, "xo25": s("xo25") / n,
               "shots": shots, "shots_a": shots_a, "sot": sot, "sot_a": per("sot_a"),
               "corners": per("corners"), "corners_a": per("corners_a")}
        out["xgd"] = out["xg"] - out["xga"]
        out["finishing"] = out["gf_tot"] - out["xg_tot"]      # + = gjort fler mål än chanserna motiverar
        out["keeping"] = out["xga_tot"] - out["ga_tot"]       # + = släppt in färre än chanserna motiverar
        out["xg_per_shot"] = out["xg"] / shots if shots else None
        out["xga_per_shot"] = out["xga"] / shots_a if shots_a else None
        out["conversion"] = out["gf"] / shots if shots else None
        out["sot_pct"] = sot / shots if shots and sot is not None else None
        return out

    def find_team(self, lg, names):
        best, score = None, 0.0
        for fd in lg["teams"]:
            n = ascii_lower(fd)
            fd_full = FD_TEAM_ALIASES.get(n, fd)
            for name in names:
                if not name:
                    continue
                s = max(similarity(name, fd), similarity(name, fd_full))
                if s > score:
                    best, score = fd, s
        return best if score >= 0.65 else None

    @staticmethod
    def xg_lambdas(lg, hh, aa):
        """
        xG-modellen: hemmalagets hemma-anfall mot bortalagets borta-försvar och tvärtom, relativt ligasnittet.
        Små urval dras mot ligasnittet (XG_SHRINK) så att tre matcher inte ger extrema värden.
        Returnerar förväntade mål (hemma, borta).
        """
        avg_h, avg_a = lg["avg_home_xg"], lg["avg_away_xg"]

        def strength(value, n, avg):
            if value is None or not avg:
                return 1.0
            return (n * value / avg + XG_SHRINK) / (n + XG_SHRINK)

        att_h = strength(hh and hh["xg"], hh and hh["n"] or 0, avg_h)
        def_a = strength(aa and aa["xga"], aa and aa["n"] or 0, avg_h)
        att_a = strength(aa and aa["xg"], aa and aa["n"] or 0, avg_a)
        def_h = strength(hh and hh["xga"], hh and hh["n"] or 0, avg_a)
        return avg_h * att_h * def_a, avg_a * att_a * def_h

    @staticmethod
    def xpts_model(lg, hh, aa):
        """
        xPts-modell: hemmalagets förväntade vinst/oavgjort/förlust per hemmamatch (xV-xO-xF, samma underlag
        som xPts) mot bortalagets per bortamatch, relativt ligans snitt. Små urval dras mot snittet.
          1 ~ hemmalagets xV × bortalagets xF / ligans hemma-xV
          X ~ hemmalagets xO × bortalagets xO / ligans xO
          2 ~ hemmalagets xF × bortalagets xV / ligans hemma-xF
        Skillnad mot xG-modellen: räknar match för match, så en enstaka match med 4 xG väger inte lika tungt.
        """
        base = lg.get("avg_home_xwdl")
        if not base or not all(base):
            return None, None
        bw, bd, bl = base

        def rates(s, prior):
            n = (s or {}).get("n") or 0
            tot = (s["xw"], s["xd"], s["xl"]) if n else (0, 0, 0)
            return tuple((tot[i] + XG_SHRINK * prior[i]) / (n + XG_SHRINK) for i in range(3))

        hw_, hd_, hl_ = rates(hh, (bw, bd, bl))            # hemmalaget hemma
        aw_, ad_, al_ = rates(aa, (bl, bd, bw))            # bortalaget borta (bortavinst = hemmaförlust)
        raw = {"one": hw_ * al_ / bw, "x": hd_ * ad_ / bd, "two": hl_ * aw_ / bl}
        tot = sum(raw.values())
        p = {k: 100 * v / tot for k, v in raw.items()}
        return p, (round(3 * p["one"] / 100 + p["x"] / 100, 2), round(3 * p["two"] / 100 + p["x"] / 100, 2))

    def enrich(self, ev):
        """Lägger till xG-statistik och en xG-modell för 1/X/2 på matchen."""
        st = ev.get("stats") or {}
        slug = st.get("espn_league") or league_slug(ev.get("league"), ev.get("country"), ev.get("country_iso"))
        lg = self.league(slug) if slug else None
        if not lg:
            return None
        names_h = [ev.get("home"), (st.get("home") or {}).get("name")]
        names_a = [ev.get("away"), (st.get("away") or {}).get("name")]
        th, ta = self.find_team(lg, names_h), self.find_team(lg, names_a)
        if not th or not ta or th == ta:
            return {"error": f"xG: hittade inte lagen i football-data ({slug})"}
        gh, ga = lg["teams"][th], lg["teams"][ta]

        def team_block(games):
            home = [g for g in games if g["home"]]
            away = [g for g in games if not g["home"]]
            return {"all": self.summarize(games), "home": self.summarize(home), "away": self.summarize(away),
                    "recent": self.summarize(games[-XG_RECENT:]),
                    "recent_venue_home": self.summarize(home[-XG_RECENT:]),
                    "recent_venue_away": self.summarize(away[-XG_RECENT:])}

        bh, ba = team_block(gh), team_block(ga)
        hh, aa = bh["home"], ba["away"]
        lam_h, lam_a = self.xg_lambdas(lg, hh, aa)
        p1, px, p2 = poisson_1x2(lam_h, lam_a)
        ex = poisson_extras(lam_h, lam_a)
        xpts_model, xpts_match = self.xpts_model(lg, hh, aa)
        tables = lg.get("tables") or {}
        return {"fd_home": th, "fd_away": ta, "home": bh, "away": ba,
                "table_home": tables.get(th), "table_away": tables.get(ta),
                "games_home": gh[-XG_RECENT:], "games_away": ga[-XG_RECENT:],
                "lambda": (round(lam_h, 2), round(lam_a, 2)),
                "model": {"one": 100 * p1, "x": 100 * px, "two": 100 * p2},
                "model_xpts": xpts_model, "xpts_match": xpts_match,
                "model_extra": {"over25": 100 * ex["over25"], "btts": 100 * ex["btts"],
                                "nil_nil": 100 * ex["nil_nil"],
                                "cs_home": 100 * ex["cs_home"], "cs_away": 100 * ex["cs_away"],
                                "top_scores": [(s, 100 * p) for s, p in ex["top_scores"]]}}


# --------------------------------------------------------------------------- #
# Reserv när ESPN saknar ligan: football-datas "extra ligor" (resultat för bl.a. Irland, Norden, Sydamerika)
# Tabell, form och vila räknas fram ur säsongens resultat. Inga skador här.
# --------------------------------------------------------------------------- #
FD_NEW_URL = "https://www.football-data.co.uk/new/{code}.csv"
FD_NEW = {  # ESPN-liga -> football-datas kod för extra ligor
    "irl.1": "IRL", "swe.1": "SWE", "nor.1": "NOR", "den.1": "DNK", "fin.1": "FIN", "pol.1": "POL",
    "aut.1": "AUT", "sui.1": "SWZ", "rou.1": "ROU", "bra.1": "BRA", "arg.1": "ARG", "usa.1": "USA",
    "jpn.1": "JPN", "chn.1": "CHN", "mex.1": "MEX", "rus.1": "RUS",
}


class FDNew:
    def __init__(self):
        self.cache = {}

    def rows(self, code):
        if code in self.cache:
            return self.cache[code]
        import csv
        import io
        CACHE_DIR.mkdir(exist_ok=True)
        path = CACHE_DIR / f"fdnew_{code}.csv"
        rows = []
        try:
            if not path.exists() or time.time() - path.stat().st_mtime > 12 * 3600:
                r = session.get(FD_NEW_URL.format(code=code), timeout=30)
                r.raise_for_status()
                path.write_bytes(r.content)
            text = path.read_bytes().decode("utf-8-sig", errors="replace")
            for x in csv.DictReader(io.StringIO(text)):
                d = x.get("Date") or ""
                for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                    try:
                        x["_date"] = datetime.strptime(d, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
                home, away = x.get("Home") or x.get("HomeTeam"), x.get("Away") or x.get("AwayTeam")
                try:
                    hg, ag = int(float(x.get("HG") or x.get("FTHG"))), int(float(x.get("AG") or x.get("FTAG")))
                except (TypeError, ValueError):
                    continue
                if "_date" in x and home and away:
                    rows.append({"date": x["_date"], "season": x.get("Season") or "", "home": home, "away": away,
                                 "hg": hg, "ag": ag})
        except (requests.RequestException, OSError) as e:
            print(f"  football-data {code}: {e}", file=sys.stderr)
        rows.sort(key=lambda r: r["date"])
        self.cache[code] = rows
        return rows

    def h2h(self, event, slug, n=5):
        """Inbördes möten från football-datas alla säsonger (ESPN saknar dem för t.ex. Allsvenskan)."""
        code = FD_NEW.get(slug)
        rows = [r for r in self.rows(code) if r["date"] < event["start"][:10]] if code else []
        if not rows:
            return []
        teams = {r["home"] for r in rows[-400:]} | {r["away"] for r in rows[-400:]}

        def find(name):
            best, sc = None, 0.0
            for t in teams:
                v = similarity(name, t)
                if v > sc:
                    best, sc = t, v
            return best if sc >= 0.6 else None

        th, ta = find(event["home"]), find(event["away"])
        if not th or not ta or th == ta:
            return []
        return [f'{r["date"]} {r["home"]} - {r["away"]} {r["hg"]}-{r["ag"]}' for r in rows
                if {r["home"], r["away"]} == {th, ta}][-n:][::-1]

    def enrich(self, event, slug):
        code = FD_NEW.get(slug)
        if not code:
            return None
        rows = self.rows(code)
        if not rows:
            return {"error": f"Ingen data hos ESPN eller football-data ({slug})"}
        before = [r for r in rows if r["date"] < event["start"][:10]]
        if not before:
            return {"error": f"Inga spelade matcher än ({slug})"}
        season = before[-1]["season"]
        cur = [r for r in before if r["season"] == season]
        teams = sorted({r["home"] for r in cur} | {r["away"] for r in cur})

        def find(name):
            best, sc = None, 0.0
            for t in teams:
                v = similarity(name, t)
                if v > sc:
                    best, sc = t, v
            return best if sc >= 0.6 else None

        th, ta = find(event["home"]), find(event["away"])
        if not th or not ta or th == ta:
            return {"error": f"Hittade inte lagen hos football-data ({code})"}
        # tabell ur säsongens resultat
        tab = {t: {"pts": 0, "gd": 0, "gf": 0, "w": 0, "d": 0, "l": 0} for t in teams}
        for r in cur:
            for t, gf, ga in ((r["home"], r["hg"], r["ag"]), (r["away"], r["ag"], r["hg"])):
                x = tab[t]
                x["gf"] += gf
                x["gd"] += gf - ga
                res = "w" if gf > ga else "d" if gf == ga else "l"
                x[res] += 1
                x["pts"] += 3 if res == "w" else 1 if res == "d" else 0
        order = sorted(teams, key=lambda t: (-tab[t]["pts"], -tab[t]["gd"], -tab[t]["gf"], t))
        table = {t: {"rank": i + 1, "group_size": len(teams)} for i, t in enumerate(order)}

        def sched(team):
            g = []
            for r in cur:
                if team not in (r["home"], r["away"]):
                    continue
                h = r["home"] == team
                gf, ga = (r["hg"], r["ag"]) if h else (r["ag"], r["hg"])
                g.append({"date": r["date"], "home": h, "opp_id": r["away"] if h else r["home"],
                          "opp_name": r["away"] if h else r["home"], "gf": gf, "ga": ga,
                          "res": "V" if gf > ga else "O" if gf == ga else "F"})
            return sorted(g, key=lambda x: x["date"], reverse=True)

        def team_info(t):
            sc = sched(t)
            x = tab[t]
            return {"form_home": weighted_form([g for g in sc if g["home"]], table),
                    "form_away": weighted_form([g for g in sc if not g["home"]], table),
                    "form_all": weighted_form(sc, table, FORM_ALL_GAMES),
                    "quartile": quartile(table[t]["rank"], len(teams)), "group_size": len(teams),
                    "id": t, "name": t, "rank": table[t]["rank"],
                    "record": {"w": x["w"], "d": x["d"], "l": x["l"], "played": x["w"] + x["d"] + x["l"],
                               "points": x["pts"]},
                    "goal_diff": x["gd"], "form": "", "last5": [], "absent": [], "injuries": [],
                    "rest": rest_days(event["start"][:10], sc)}

        h2h = [f'{r["date"]} {r["home"]} - {r["away"]} {r["hg"]}-{r["ag"]}' for r in before
               if {r["home"], r["away"]} == {th, ta}][-5:][::-1]
        return {"espn_league": slug, "league_name": f"{code} (football-data)", "status": None,
                "source": "football-data", "home": team_info(th), "away": team_info(ta), "h2h": h2h}


# --------------------------------------------------------------------------- #
# Analys per match: oddsrörelser, streck mot odds, flaggor
# --------------------------------------------------------------------------- #
SIGNS = {"one": "1", "x": "X", "two": "2"}


def implied_pct(t):
    """Odds -> sannolikhet i procent utan bookmakerns marginal."""
    if not t or not all(t.get(k) for k in SIGNS):
        return None
    inv = {k: 1 / t[k] for k in SIGNS}
    s = sum(inv.values())
    return {k: 100 * v / s for k, v in inv.items()}


def fmt_odds(v):
    return f"{v:.2f}".replace(".", ",") if v else "-"


def prognos_weights(has_xg, has_xpts):
    """Vikter (odds, xPts, xG). Saknas en modell tar den andra dess vikt; saknas båda blir det bara odds."""
    wo, wp, wx = PROGNOS_ODDS_WEIGHT, PROGNOS_XPTS_WEIGHT, PROGNOS_XG_WEIGHT
    if has_xg and has_xpts:
        return wo, wp, wx
    if has_xpts and wp:
        return wo, wp + wx, 0.0
    if has_xg and wx:
        return wo, 0.0, wp + wx
    return 1.0, 0.0, 0.0


def prognos_label():
    parts = [f"odds {PROGNOS_ODDS_WEIGHT:.0%}"]
    if PROGNOS_XPTS_WEIGHT:
        parts.append(f"xPts-modell {PROGNOS_XPTS_WEIGHT:.0%}")
    if PROGNOS_XG_WEIGHT:
        parts.append(f"xG-modell {PROGNOS_XG_WEIGHT:.0%}")
    kr = ", krysset från oddsen" if PROGNOS_KRYSS_FRAN_ODDS and PROGNOS_ODDS_WEIGHT < 1 else ""
    fj = f" + formjustering (max ±{FORM_ADJ_MAX:g} %-enheter)" if FORM_ADJ_K else ""
    return "prognos = " + " + ".join(parts) + kr + fj


def form_adjust(p_now, home, away):
    """
    Flyttar sannolikhet mellan 1:an och 2:an efter skillnaden i viktad medelpoäng (8 senaste).
    Returnerar (justerade sannolikheter, skift i procentenheter för 1:an) eller (p_now, None).
    """
    if not FORM_ADJ_K or not p_now:
        return p_now, None
    if (home or {}).get("form_league") != (away or {}).get("form_league"):
        return p_now, None   # olika ligor (Europacup) - formbetygen går inte att jämföra
    fh, fa = (home or {}).get("form_all") or {}, (away or {}).get("form_all") or {}
    if fh.get("mean") is None or fa.get("mean") is None:
        return p_now, None
    if len(fh.get("games") or []) < FORM_ADJ_MIN_GAMES or len(fa.get("games") or []) < FORM_ADJ_MIN_GAMES:
        return p_now, None
    shift = max(-FORM_ADJ_MAX, min(FORM_ADJ_MAX, FORM_ADJ_K * (fh["mean"] - fa["mean"])))
    # ingen sannolikhet får gå under 1 %
    shift = min(shift, p_now["two"] - 1) if shift > 0 else max(shift, -(p_now["one"] - 1))
    return {"one": p_now["one"] + shift, "x": p_now["x"], "two": p_now["two"] - shift}, round(shift, 1)


def prognos(p_now, model, folk, model_xpts=None):
    """
    Vägd sannolikhet (odds + xPts-modell + xG-modell), tipsförslag och spelvärde mot folket.
    Returnerar None om oddsen saknas.
    """
    if not p_now:
        return None
    wo, wp, wx = prognos_weights(bool(model), bool(model_xpts))
    p = {k: wo * p_now[k] + (wp * model_xpts[k] if wp else 0) + (wx * model[k] if wx else 0) for k in SIGNS}
    if PROGNOS_KRYSS_FRAN_ODDS and wo < 1 and p["one"] + p["two"] > 0:
        rest = 100 - p_now["x"]
        f = rest / (p["one"] + p["two"])
        p = {"one": p["one"] * f, "x": p_now["x"], "two": p["two"] * f}
    order = sorted(SIGNS, key=lambda k: -p[k])
    if p[order[0]] >= SPIK_MIN:
        tips = [order[0]]
    elif p[order[0]] + p[order[1]] >= HALV_MIN and not ("x" not in order[:2] and p["x"] >= p[order[1]] - 5):
        tips = order[:2]      # (1 och 2 med krysset nästan lika troligt blir hellre helgardering)
    else:
        tips = list(order)

    value = {}
    if folk and all(folk.get(k) for k in SIGNS):
        for k in SIGNS:
            value[k] = p[k] / folk[k]
    value_signs = [k for k in sorted(SIGNS, key=lambda k: -value.get(k, 0))
                   if value.get(k, 0) >= VALUE_MIN_RATIO and p[k] >= VALUE_MIN_PROB]
    # Förslag med värde: lägg till det mest understreckade tecknet om det inte redan är med
    tips_value = list(tips)
    if value_signs and value_signs[0] not in tips_value and len(tips_value) < 3:
        tips_value.append(value_signs[0])
    # ... och ett spikat tecken som folket överstreckar kraftigt gardera hellre
    if len(tips_value) == 1 and value.get(tips_value[0], 1) < 0.8:
        tips_value.append(order[1])

    def fmt(ts):
        return "".join(SIGNS[k] for k in ("one", "x", "two") if k in ts)

    top, second = order[0], order[1]
    pick = SIGNS[top] if p[top] >= TIPS_MIN and p[top] - p[second] >= TIPS_LEAD else "1X2"
    return {"p": p, "order": order, "pick": pick, "tips": fmt(tips), "tips_value": fmt(tips_value),
            "n_tips": len(tips), "n_tips_value": len(tips_value),
            "value": value, "value_signs": value_signs, "used_model": bool(model)}


def absent_why(p):
    """Kort orsak: 'avstängd', 'knä', 'osäker' ..."""
    s = (p.get("status") or "").lower()
    r = p.get("reason") or ""
    if "susp" in s or "avst" in s:
        return "avstängd"
    if "day" in s or "question" in s or "doubt" in s:
        return f"osäker{' - ' + r if r else ''}"
    return r or ("skadad" if s else "")


def analyse_event(ev):
    p_now, p_start = implied_pct(ev["odds"]), implied_pct(ev.get("start_odds"))
    folk = ev.get("svenska_folket") or {}
    st = ev.get("stats") or {}
    home, away = st.get("home") or {}, st.get("away") or {}
    flags = []
    if ev.get("odds_from_start"):
        flags.append(("odds", "Aktuellt odds saknas (borttaget nära avspark) - prognosen bygger på startoddset"))
    elif ev.get("odds_from_saved"):
        flags.append(("odds", f'Aktuellt odds saknas (borttaget nära avspark) - prognosen bygger på oddset från '
                              f'din körning kl {ev["odds_from_saved"]}'))

    move = {k: p_now[k] - p_start[k] for k in SIGNS} if p_now and p_start else {}
    streck = {k: folk[k] - p_now[k] for k in SIGNS} if p_now and all(folk.get(k) is not None for k in SIGNS) else {}
    handled = set()   # tecken som redan fått den kombinerade flaggan

    # 1) Oddset har rört sig men folket har inte hängt med - den viktigaste flaggan för poolspel.
    #    Backtestet visade att kraftiga oddsrörelser pekar rätt; folket streckar ofta tidigt och ändrar sig inte.
    for k, d in sorted(move.items(), key=lambda x: -abs(x[1])):
        s = streck.get(k)
        if abs(d) < FLAG_ODDS_MOVE or s is None:
            continue
        change = f'{fmt_odds(ev["start_odds"][k])} → {fmt_odds(ev["odds"][k])}'
        if d > 0 and s <= -FLAG_FOLLOW:
            flags.append(("folj", f'Oddset på {SIGNS[k]} har sjunkit {change}, men folket har inte hängt med: '
                                  f'folket {folk[k]:.0f} %, oddsen nu {p_now[k]:.0f} % - {SIGNS[k]}:an understreckad'))
            handled.add(k)
        elif d < 0 and s >= FLAG_FOLLOW:
            flags.append(("folj", f'Oddset på {SIGNS[k]} har stigit {change}, men folket streckar fortfarande '
                                  f'{folk[k]:.0f} % mot oddsens {p_now[k]:.0f} % - {SIGNS[k]}:an överstreckad'))
            handled.add(k)

    # 2) Övriga oddsrörelser
    for k, d in sorted(move.items(), key=lambda x: -abs(x[1])):
        if abs(d) >= FLAG_ODDS_MOVE and k not in handled:
            how = "sjunkit" if d > 0 else "stigit"
            flags.append(("odds", f'Oddset på {SIGNS[k]} har {how} {fmt_odds(ev["start_odds"][k])} → '
                                  f'{fmt_odds(ev["odds"][k])} ({d:+.1f} %-enheter)'))

    # 3) Övrigt över- och understreck
    for k, d in sorted(streck.items(), key=lambda x: -abs(x[1])):
        if abs(d) >= FLAG_STRECK and k not in handled:
            what = "överstreckad" if d > 0 else "understreckad"
            flags.append(("streck", f'{SIGNS[k]}:an {what}: folket {folk[k]:.0f} %, oddsen {p_now[k]:.0f} %'))

    # 4) xG: modellen mot oddsen, och lag som tagit mer/mindre poäng än chanserna motiverar
    xg = ev.get("xg") or {}
    model = xg.get("model")
    model_xpts = xg.get("model_xpts")
    if p_now:
        # Bara den största skillnaden (1:an och 2:an speglar oftast varandra), den tyngst vägda modellen först
        order = [("xPts-modellen", model_xpts, PROGNOS_XPTS_WEIGHT), ("xG-modellen", model, PROGNOS_XG_WEIGHT)]
        for mname, mod, _w in sorted(order, key=lambda o: -o[2]):
            if not mod:
                continue
            k = max(SIGNS, key=lambda s: abs(mod[s] - p_now[s]))
            if abs(mod[k] - p_now[k]) >= FLAG_XG:
                more = "högre" if mod[k] > p_now[k] else "lägre"
                flags.append(("xg", f'{mname} ger {SIGNS[k]}:an {mod[k]:.0f} % - {more} än oddsens {p_now[k]:.0f} %'))
                break
    for side, fallback in (("home", ev.get("home")), ("away", ev.get("away"))):
        s = ((xg.get(side) or {}).get("all")) or {}
        tb = xg.get(f"table_{side}") or {}
        if s.get("n", 0) >= FLAG_LUCK_MIN:
            d = s["pts"] - s["xpts"]
            name = (st.get(side) or {}).get("name") or fallback
            where = (f', {tb["rank_all"]}:a i tabellen men {tb["xrank_all"]}:a i xG-tabellen'
                     if tb and abs(tb["rank_all"] - tb["xrank_all"]) >= FLAG_XTABLE else "")
            if d >= FLAG_LUCK:
                flags.append(("xg", f'{name} har tagit {d:+.1f} p/match mer än xG motiverar ({s["pts"]:.1f} mot '
                                    f'{s["xpts"]:.1f}{where}) - överpresterar?'))
            elif d <= -FLAG_LUCK:
                flags.append(("xg", f'{name} har tagit {abs(d):.1f} p/match mindre än xG motiverar ({s["pts"]:.1f} mot '
                                    f'{s["xpts"]:.1f}{where}) - bättre än tabellen visar?'))
            elif where:
                flags.append(("xg", f'{name}{where.replace(", ", " är ", 1)}'))

    fh = (home.get("form_home") or {}).get("per_game")
    fa = (away.get("form_away") or {}).get("per_game")
    if st.get("cross_league"):
        fh = fa = None   # olika ligor - formen jämförs inte
    form_diff = fh - fa if fh is not None and fa is not None else None
    if form_diff is not None and folk.get("one") is not None and folk.get("two") is not None:
        if form_diff >= FLAG_FORM and folk["two"] >= folk["one"]:
            flags.append(("form", f'Formen talar för {home.get("name")} (formbetyg {grade_s(fh)} hemma mot {grade_s(fa)} borta), '
                                  f'men folket tror mer på 2:an ({folk["two"]:.0f} % mot {folk["one"]:.0f} %)'))
        elif form_diff <= -FLAG_FORM and folk["one"] >= folk["two"]:
            flags.append(("form", f'Formen talar för {away.get("name")} (formbetyg {grade_s(fa)} borta mot {grade_s(fh)} hemma), '
                                  f'men folket tror mer på 1:an ({folk["one"]:.0f} % mot {folk["two"]:.0f} %)'))

    rh, ra = home.get("rest"), away.get("rest")
    league = (st.get("league_name") or "").lower()

    def cup_this_week(r):
        if r and domestic_slug([{"competition": r["competition"]}]) and "uefa" in (st.get("espn_league") or ""):
            return False   # Europacupmatch: ligamatchen i helgen är vardag för båda lagen
        return bool(r and r["days"] <= 7 and r["competition"] not in ("liga", "")
                    and league and r["competition"].lower() not in league)

    def when(r):
        wd = ["måndags", "tisdags", "onsdags", "torsdags", "fredags", "lördags", "söndags"]
        return f'i {wd[datetime.fromisoformat(r["date"]).weekday()]}'

    cup_h, cup_a = cup_this_week(rh), cup_this_week(ra)
    rest_flagged = False
    if rh and ra and abs(rh["days"] - ra["days"]) >= REST_FLAG_DAYS and min(rh["days"], ra["days"]) <= 7:
        (t, r_t), (o, r_o) = ((home, rh), (away, ra)) if rh["days"] > ra["days"] else ((away, ra), (home, rh))
        text = f'{t.get("name")} har {r_t["days"] - r_o["days"]} dagar mer vila'
        if cup_this_week(r_o) and not cup_this_week(r_t):
            text += f' - {o.get("name")} spelade {r_o["competition"]} {when(r_o)}'
        else:
            text += f' än {o.get("name")}'
        flags.append(("vila", text))
        rest_flagged = True
    # Cupmatch i veckan flaggas bara när ett av lagen spelat (spelade båda är det lika för båda)
    if cup_h != cup_a and not rest_flagged:
        t, r = (home, rh) if cup_h else (away, ra)
        flags.append(("vila", f'{t.get("name")} spelade {r["competition"]} {when(r)} - rotation?'))
    for t, r in ((home, rh), (away, ra)):
        if r and r["days"] <= FLAG_SHORT_REST:
            flags.append(("vila", f'{t.get("name")} spelade för bara {r["days"]} dagar sedan'))

    for t in (home, away):
        key = [p for p in t.get("absent") or [] if (p.get("start_pct") or 0) >= KEY_PLAYER_PCT]
        if key:
            names = ", ".join(f'{p["name"]} ({p["starts"]}/{p["games"]} starter{", " + absent_why(p) if absent_why(p) else ""})'
                              for p in key[:4])
            flags.append(("skada", f'{t.get("name")} saknar {len(key)} ordinarie: {names}'))
    if st.get("status"):
        flags.append(("status", f'Status hos ESPN: {st["status"]}'))
    if st.get("error"):
        flags.append(("status", st["error"]))

    p_form, form_shift = form_adjust(p_now, home, away)
    pg = prognos(p_form, model, folk, model_xpts)
    if pg is not None:
        pg["form_shift"] = form_shift
        if form_shift and abs(form_shift) >= 2:
            who = home.get("name") if form_shift > 0 else away.get("name")
            f8h, f8a = home["form_all"], away["form_all"]
            flags.append(("form", f'Formen talar för {who}: formbetyg {grade_s(f8h["mean"])} mot {grade_s(f8a["mean"])} av 10 '
                                  f'(8 senaste) - prognosen flyttad {abs(form_shift):.0f} %-enheter'))
    if pg and pg["value_signs"]:
        k = pg["value_signs"][0]
        fav = pg["order"][0]
        text = (f'Spelvärde på {SIGNS[k]}:an: vår sannolikhet {pg["p"][k]:.0f} % mot folkets {folk[k]:.0f} %')
        if k != fav:
            text += f' - statistiken tror mest på {SIGNS[fav]}:an ({pg["p"][fav]:.0f} %), men {SIGNS[k]}:an är underspelad'
        flags.insert(0, ("varde", text))
        # samma sak behöver inte sägas två gånger
        flags = [f for f in flags if not (f[0] == "streck" and f[1].startswith(f"{SIGNS[k]}:an understreckad"))]
    ev["analysis"] = {"p_now": p_now, "p_start": p_start, "move": move, "streck": streck, "xg_model": model,
                      "xpts_model": model_xpts,
                      "prognos": pg,
                      "form_home": fh, "form_away": fa, "form_diff": form_diff,
                      "rest_home": rh and rh["days"], "rest_away": ra and ra["days"], "flags": flags}
    return ev["analysis"]


def short_name(t, fallback, width=12):
    n = (t or {}).get("name") or fallback or ""
    return n if len(n) <= width else n[:width - 1] + "."


SKRALL_MIN_PROB = 15.0     # skrällkupongen garderar bara med tecken som har minst så här stor chans (%)
KRYSS_VIKT = 0.0           # extra vikt på X när kupongen byggs, t.ex. 20 = X räknas 20 % troligare (--kryss)


# Systemval (--system eller frågan vid start). Tomt = automatiskt efter radbudget.
#   hel/halv : exakt antal hel- och halvgarderingar (övriga spikas)
#   garanti  : reducera spelkupongen så att minst så många rätt garanteras om rätt rad finns i systemet
#   max      : högst så många rader i den reducerade spelkupongen (de troligaste behålls)
SYSTEM = {}
# Förvalt system per spel när du bara trycker Enter (ändra fritt). Tomt {} = automatiskt efter radbudget.
DEFAULT_SYSTEMS = {
    "topptipset": {"hel": 2, "halv": 2},     # 36 rader (topp_backtest: 2-2 Säkrast gav bäst återbetalning)
    "stryktipset": {"hel": 0, "halv": 7},    # 128 rader
    "europatipset": {"hel": 0, "halv": 7},   # 128 rader
}


AUTO_SYSTEM_PRODUCTS = {"stryktipset", "europatipset"}   # här väljer webbsidan själv bästa systemet för pengarna


def default_system(draw):
    product = str(draw.get("product", ""))
    for prefix, sysval in DEFAULT_SYSTEMS.items():
        if product.startswith(prefix):
            return dict(sysval)
    return {}
REDUCE_MAX_SYSTEM = 60000   # större system än så reduceras inte (tar för lång tid)


def parse_system(text):
    """
    Tolkar t.ex. '4 hel 3 halv', '4h3b', '4-3', '4-3 r12', '5-4 r11 max 300', '200' (radbudget).
    Returnerar dict eller kastar ValueError.
    """
    s = (text or "").lower().replace(",", " ").strip()
    if not s:
        return {}
    out = {}
    m = re.search(r"\br\s*(\d{1,2})\b|garanti\s*(\d{1,2})", s)
    if m:
        out["garanti"] = int(m.group(1) or m.group(2))
        s = s[:m.start()] + " " + s[m.end():]
    m = re.search(r"max\s*(\d+)", s)
    if m:
        out["max"] = int(m.group(1))
        s = s[:m.start()] + " " + s[m.end():]
    hel = re.search(r"(\d+)\s*(?:hel[a-zåäö]*|h(?![a-zåäö]))", s)
    halv = re.search(r"(\d+)\s*(?:halv[a-zåäö]*|b(?![a-zåäö]))", s)
    dash = re.search(r"^\s*(\d+)\s*-\s*(\d+)\s*$", s)
    if dash:
        out["hel"], out["halv"] = int(dash.group(1)), int(dash.group(2))
    elif hel or halv:
        out["hel"] = int(hel.group(1)) if hel else 0
        out["halv"] = int(halv.group(1)) if halv else 0
    elif re.fullmatch(r"\s*\d+\s*", s):
        out["rader"] = int(s)
    elif s.strip():
        raise ValueError(f"förstår inte '{text}'")
    if ("garanti" in out or "max" in out) and "hel" not in out:
        raise ValueError("reducering kräver ett system, t.ex. '4-3 r12'")
    return out


def system_label():
    kv = f", kryssvikt +{KRYSS_VIKT:g} %" if KRYSS_VIKT else ""
    return _system_label() + kv


def _system_label():
    if SYSTEM.get("hel") is None:
        return f"upp till {RADER} rader"
    rows = 3 ** SYSTEM["hel"] * 2 ** SYSTEM["halv"]
    lab = f'{SYSTEM["hel"]} hel + {SYSTEM["halv"]} halv = {rows} rader'
    if SYSTEM.get("garanti") or SYSTEM.get("max"):
        lab += " (reduceras"
        lab += f', garanti {SYSTEM["garanti"]} rätt' if SYSTEM.get("garanti") else ""
        lab += f', max {SYSTEM["max"]} rader' if SYSTEM.get("max") else ""
        lab += ")"
    return lab


def kryss_adjust(p, v, kryss):
    """Höjer X med `kryss` procent och normaliserar om. Spelvärdet följer med (v = p / streck)."""
    if not kryss:
        return p, v
    raw = {k: p[k] * (1 + kryss / 100 if k == "x" else 1) for k in SIGNS}
    tot = sum(raw.values()) or 1
    p2 = {k: 100 * raw[k] / tot for k in SIGNS}
    v2 = {k: v[k] * p2[k] / p[k] for k in v if p.get(k)}
    return p2, v2


def _coupon_weights(draw, with_value=False, skrall=False, kryss=None):
    kryss = KRYSS_VIKT if kryss is None else kryss
    probs, weights, chosen = {}, {}, {}
    for ev in draw["events"]:
        pg = (ev.get("analysis") or {}).get("prognos")
        p = pg["p"] if pg else {k: 100 / 3 for k in SIGNS}
        v = (pg or {}).get("value") or {}
        probs[ev["nr"]] = p                      # riktiga sannolikheten (för chans-uträkningen)
        p, v = kryss_adjust(p, v, kryss)         # kryssviktad (för val av tecken)
        if skrall:
            weights[ev["nr"]] = {k: (v.get(k, p[k] / 33.3) if p[k] >= SKRALL_MIN_PROB else 0.0) for k in SIGNS}
        else:
            weights[ev["nr"]] = {k: p[k] * (min(v.get(k, 1.0), 2.0) if with_value else 1.0) for k in SIGNS}
        chosen[ev["nr"]] = [max(SIGNS, key=lambda k: p[k])]      # alltid det troligaste tecknet först
        if skrall and weights[ev["nr"]][chosen[ev["nr"]][0]] <= 0:
            weights[ev["nr"]][chosen[ev["nr"]][0]] = 0.01
    return probs, weights, chosen


def build_system(draw, hel, halv, with_value=False, skrall=False, kryss=None):
    """
    Fast system: exakt `hel` helgarderingar och `halv` halvgarderingar. Väljer vilka matcher som får vad
    så att produkten av vikterna (samma vikter som build_coupon) blir störst - löses exakt med dynamisk
    programmering över matcherna.
    """
    import math
    probs, weights, chosen = _coupon_weights(draw, with_value, skrall, kryss)
    nrs = list(chosen)
    if hel + halv > len(nrs):
        raise ValueError(f"{hel} hel + {halv} halv är fler än {len(nrs)} matcher")
    opts = {}
    for nr in nrs:
        fav = chosen[nr][0]
        w = weights[nr]
        others = sorted((k for k in SIGNS if k != fav), key=lambda k: -w[k])
        lg = lambda ks: math.log(max(sum(w[k] for k in ks), 1e-9))
        opts[nr] = {0: ([fav], lg([fav])), 1: ([fav, others[0]], lg([fav, others[0]])),
                    2: ([fav] + others, lg([fav] + others))}
    NEG = float("-inf")
    dp = {(0, 0): (0.0, [])}
    for nr in nrs:
        nxt = {}
        for (h, b), (sc, path) in dp.items():
            for kind, dh, db in ((0, 0, 0), (1, 0, 1), (2, 1, 0)):
                key = (h + dh, b + db)
                if key[0] > hel or key[1] > halv:
                    continue
                val = sc + opts[nr][kind][1]
                if val > nxt.get(key, (NEG,))[0]:
                    nxt[key] = (val, path + [kind])
        dp = nxt
    _, path = dp[(hel, halv)]
    fmt = lambda ss: "".join(SIGNS[k] for k in ("one", "x", "two") if k in ss)
    coupon, cover = {}, 1.0
    for nr, kind in zip(nrs, path):
        ss = opts[nr][kind][0]
        coupon[nr] = fmt(ss)
        cover *= sum(probs[nr][k] for k in ss) / 100
    return coupon, 3 ** hel * 2 ** halv, cover


def _row_probs(draw):
    """Sannolikhet (0-1) per match och tecken ('1','X','2') enligt prognosen."""
    out = []
    for ev in draw["events"]:
        pg = (ev.get("analysis") or {}).get("prognos")
        p = pg["p"] if pg else {k: 100 / 3 for k in SIGNS}
        out.append({SIGNS[k]: p[k] / 100 for k in SIGNS})
    return out


def reduce_system(draw, coupon, garanti=None, max_rows=None):
    """
    Reducerar ett fullt system till färre rader.
      garanti : om rätt rad finns bland systemets rader har minst en spelad rad minst `garanti` rätt
                (12 = högst ett fel, 11 = högst två fel). Byggs girigt: nästa rad är alltid den som
                täcker mest ännu otäckt sannolikhet, så de troligaste raderna täcks först.
      max     : högst så många rader. Utan garanti behålls de troligaste raderna.
    Returnerar lista med rader (strängar som '1X21...').
    """
    import heapq
    import itertools
    probs = _row_probs(draw)
    signs = [list(coupon[ev["nr"]]) for ev in draw["events"]]
    full = 1
    for s in signs:
        full *= len(s)
    if full > 5000 and garanti:
        print(f"  Reducerar {full} rader, det kan ta en halv minut ...", flush=True)
    if full > REDUCE_MAX_SYSTEM:
        raise ValueError(f"systemet har {full} rader - för stort att reducera (max {REDUCE_MAX_SYSTEM})")
    rows = ["".join(r) for r in itertools.product(*signs)]
    rp = {}
    for r in rows:
        p = 1.0
        for i, c in enumerate(r):
            p *= probs[i][c]
        rp[r] = p
    if not garanti:
        keep = sorted(rows, key=lambda r: -rp[r])
        return keep[:max_rows] if max_rows else keep
    radius = len(signs) - garanti
    if not 1 <= radius <= 3:
        raise ValueError(f"garanti {garanti} rätt fungerar inte med {len(signs)} matcher")
    gpos = [i for i, s in enumerate(signs) if len(s) > 1]

    def ball(r):
        out = [r]
        for d in range(1, radius + 1):
            for pos in itertools.combinations(gpos, d):
                alts = [[c for c in signs[i] if c != r[i]] for i in pos]
                for repl in itertools.product(*alts):
                    lst = list(r)
                    for i, c in zip(pos, repl):
                        lst[i] = c
                    out.append("".join(lst))
        return out

    uncovered = set(rows)
    gain = lambda r: sum(rp[x] for x in ball(r) if x in uncovered)
    heap = [(-gain(r), r) for r in rows]
    heapq.heapify(heap)
    chosen = []
    while uncovered and heap and (not max_rows or len(chosen) < max_rows):
        g, r = heapq.heappop(heap)
        g_now = gain(r)
        if g_now <= 0:
            continue
        if heap and g_now < -heap[0][0] - 1e-18:
            heapq.heappush(heap, (-g_now, r))          # lat girig: värdet har sjunkit, prova igen senare
            continue
        chosen.append(r)
        uncovered.difference_update(ball(r))
    return chosen


def row_chances(draw, played_rows):
    """Chans (0-1) för 13, minst 12 och minst 11 rätt med de spelade raderna, enligt prognosen."""
    import itertools
    probs = _row_probs(draw)
    n = len(probs)
    res = {}
    seen = set()
    for dist in range(0, 3):
        for r in played_rows:
            for pos in itertools.combinations(range(n), dist):
                alts = [[c for c in "1X2" if c != r[i]] for i in pos]
                for repl in itertools.product(*alts):
                    lst = list(r)
                    for i, c in zip(pos, repl):
                        lst[i] = c
                    seen.add("".join(lst))
        tot = 0.0
        for o in seen:
            p = 1.0
            for i, c in enumerate(o):
                p *= probs[i][c]
            tot += p
        res[n - dist] = tot
    return res


def write_rows_file(draw, rows, path):
    """
    Sparar raderna som textfil. Första raden är spelets namn, sedan en rad per spelrad i formatet
    E,1,X,2,... - det format som brukar användas för att ladda upp egna rader. Kontrollera mot
    Svenska Spels instruktioner innan du laddar upp.
    """
    name = {"stryktipset": "Stryktipset", "europatipset": "Europatipset"}.get(draw["product"], draw["product"].capitalize())
    lines = [name] + ["E," + ",".join(r) for r in rows]
    Path(path).write_text("\n".join(lines) + "\n", "utf-8")
    return path


def play_strategy(draw):
    """
    Vilken kupong som föreslås att spela. Backtesten:
      Stryktipset (113 omgångar): Skräll gav mest tillbaka.
      Topptipset (357 omgångar): Säkrast gav 77-85 % på 2-2 till 3-3, Skräll klart sämre (bara 8 matcher
      ger för få träffar för att skrällvinsterna ska hinna komma). Därför Säkrast på Topptipset.
    """
    #   Stryktipset med fasta system (115 omgångar): på 0-7 gav Med värde mest (31 %), Skräll 21 %.
    #   Med värde tar med understreckade tecken - lite skrällar, men inte bara skrällar.
    return "safe" if str(draw.get("product", "")).startswith("topptipset") else "value"


def play_coupon(draw):
    """Kupongen att spela (Säkrast, Med värde eller Skräll, se play_strategy), ev. reducerad."""
    st = play_strategy(draw)
    skr, rows_k, cov = build_coupon(draw, skrall=st == "skrall", with_value=st == "value")
    if SYSTEM.get("garanti") or SYSTEM.get("max"):
        red = reduce_system(draw, skr, SYSTEM.get("garanti"), SYSTEM.get("max"))
        return skr, red, len(red), row_chances(draw, red)
    return skr, None, rows_k, None


def build_coupon(draw, budget=None, with_value=False, skrall=False, kryss=None):
    """
    Lägger garderingar inom radbudgeten.
      standard     : där de ökar chansen till alla rätt mest per extra rad
      with_value   : sannolikhet × spelvärde, så understreckade tecken prioriteras
      skrall       : sannolikhet / streck - maximerar förväntad utdelning vid 13 rätt, eftersom utdelningen
                     är ungefär potten delat med antalet vinnare (som följer folkets streck)
    Returnerar {nr: "1X"}, antal rader och chansen (enligt prognosen) att alla tecken finns med.
    """
    import math
    if SYSTEM.get("hel") is not None:
        return build_system(draw, SYSTEM["hel"], SYSTEM["halv"], with_value, skrall, kryss)
    budget = budget or RADER
    probs, weights, chosen = _coupon_weights(draw, with_value, skrall, kryss)
    rows = 1
    while True:
        best, best_gain = None, 0.0
        for nr, signs in chosen.items():
            n = len(signs)
            if n == 3 or rows // n * (n + 1) > budget:
                continue
            s = sum(weights[nr][k] for k in signs)
            for k in SIGNS:
                if k in signs:
                    continue
                if weights[nr][k] <= 0:
                    continue
                gain = math.log((s + weights[nr][k]) / s) / math.log((n + 1) / n)
                if gain > best_gain:
                    best, best_gain = (nr, k), gain
        if not best:
            break
        nr, k = best
        rows = rows // len(chosen[nr]) * (len(chosen[nr]) + 1)
        chosen[nr].append(k)
    cover = 1.0
    for nr, signs in chosen.items():
        cover *= sum(probs[nr][k] for k in signs) / 100
    fmt = lambda ss: "".join(SIGNS[k] for k in ("one", "x", "two") if k in ss)
    return {nr: fmt(ss) for nr, ss in chosen.items()}, rows, cover


def payout_index(draw, coupon):
    """
    Genomsnittlig värdekvot per rad: medel över raderna av produkten (sannolikhet / streck).
    Över 1 = kupongens rader har i snitt bättre förväntad utdelning vid 13 rätt än ett 'rättvist' spel.
    """
    idx, rows = 1.0, 1
    for ev in draw["events"]:
        pg = (ev.get("analysis") or {}).get("prognos") or {}
        v = pg.get("value") or {}
        signs = [k for k in SIGNS if SIGNS[k] in coupon[ev["nr"]]]
        idx *= sum(v.get(k, 1.0) for k in signs)
        rows *= len(signs)
    return idx / rows if rows else None


def coupon_rows(draw, key):
    rows = 1
    for ev in draw["events"]:
        pg = (ev.get("analysis") or {}).get("prognos")
        rows *= pg[key] if pg else 3
    return rows


def print_coupon(draw):
    if not any((ev.get("analysis") or {}).get("prognos") for ev in draw["events"]):
        return
    safe, rows_s, cov_s = build_coupon(draw)
    val, rows_v, cov_v = build_coupon(draw, with_value=True)
    skr, rows_k, cov_k = build_coupon(draw, skrall=True)
    print(f"\n  KUPONGFÖRSLAG  {system_label()}  ({prognos_label()})")
    print("  Nr Match                          Prognos 1/X/2   Tips  Bedömning  Säkrast  Med värde  Skräll   Spelvärde")
    for ev in draw["events"]:
        pg = (ev.get("analysis") or {}).get("prognos")
        match = match_name(ev)
        nr = ev["nr"]
        if not pg:
            print(f'  {nr:>2} {match[:30]:30}  {"-":14}  {"-":4}  {"-":9}  {safe[nr]:<7}  {val[nr]:<9}  {skr[nr]:<7}')
            continue
        p = pg["p"]
        vs = ", ".join(f'{SIGNS[k]}:an {pg["value"][k]:.2f}'.replace(".", ",") for k in pg["value_signs"])
        print(f'  {nr:>2} {match[:30]:30}  {p["one"]:3.0f}/{p["x"]:2.0f}/{p["two"]:3.0f}     {pg["pick"]:<4}  '
              f'{pg["tips"]:<9}  {safe[nr]:<7}  {val[nr]:<9}  {skr[nr]:<7}  {vs}')
    for name, rows_, cov_, cp in (("Säkrast", rows_s, cov_s, safe), ("Med värde", rows_v, cov_v, val),
                                  ("Skräll", rows_k, cov_k, skr)):
        pi = payout_index(draw, cp)
        print(f'  {name:9} {rows_:3} rader, chans för {len(draw["events"])} rätt {100 * cov_:4.1f} %, '
              f'värdekvot per rad {pi:.2f}'.replace(".", ","))
    play, play_rows, play_name = {"safe": (safe, rows_s, "Säkrast"), "value": (val, rows_v, "Med värde"),
                                  "skrall": (skr, rows_k, "Skräll")}[play_strategy(draw)]
    print(f'\n  >>> ATT SPELA ({play_name}, {play_rows} rader):  '
          + "  ".join(f'{ev["nr"]}:{play[ev["nr"]]}' for ev in draw["events"]))
    red = draw.get("_played")
    if red and red[1] is not None:
        n = len(draw["events"])
        ch = red[3]
        pct = lambda x, d: f"{100 * x:.{d}f}".replace(".", ",")
        print(f'  >>> REDUCERAT till {red[2]} rader (av {play_rows}). Chans enligt prognosen: '
              f'{n} rätt {pct(ch[n], 2)} %, {n - 1}+ {pct(ch[n - 1], 1)} %, {n - 2}+ {pct(ch[n - 2], 1)} %')
        print("      Raderna sparas i en textfil bredvid webbsidan (se 'Rader:' nedan).")
    print("  Värdekvot per rad = förväntad utdelning jämfört med ett 'rättvist' spel. Skräll satsar på högre kvot,")
    print("  säkrast på högre chans - backtestet visade att favoritkuponger mest träffar när utdelningen är låg.")
    print(f'  Bedömning = matchen för sig (spik från {SPIK_MIN:.0f} %, halv när två tecken når {HALV_MIN:.0f} %). '
          f'Spelvärde = vår sannolikhet / folkets streck. Byt radbudget med --rader.')


EN_SEP = " – "


def team_pos(ev, side):
    """Tabellplacering som heltal (ESPN skickar t.ex. 16.0), eller None."""
    rank = (((ev.get("stats") or {}).get(side)) or {}).get("rank")
    try:
        return int(round(float(rank))) if rank is not None else None
    except (TypeError, ValueError):
        return None


def team_rank(ev, side):
    """Lagnamn med tabellplacering inom parentes, t.ex. 'Shamrock Rovers (2)'."""
    pos = team_pos(ev, side)
    return f"{ev[side]} ({pos})" if pos is not None else ev[side]


def match_name(ev, sep=" - "):
    return f'{team_rank(ev, "home")}{sep}{team_rank(ev, "away")}'


def match_html(ev):
    """Som match_name men med placeringen i mindre, grå stil."""
    def one(side):
        pos = team_pos(ev, side)
        return _e(ev[side]) + (f' <span class="rk">({pos})</span>' if pos is not None else "")
    return f'{one("home")} – {one("away")}'


def print_summary(draw):
    print("\n  Nr Match                          Tabell  Form hem/bort   Vila    Odds 1 / X / 2 (rörelse)       Folket 1/X/2   !")
    print("  " + "-" * 116)
    for ev in draw["events"]:
        a = ev.get("analysis") or {}
        st = ev.get("stats") or {}
        h, aw = st.get("home") or {}, st.get("away") or {}
        match = match_name(ev)
        tab = f'{num(h.get("rank")) if h.get("rank") is not None else "-"}-{num(aw.get("rank")) if aw.get("rank") is not None else "-"}'
        form = "      -      " if a.get("form_diff") is None else f'{grade_s(a["form_home"]):>4} / {grade_s(a["form_away"]):<4}'
        rest = f'{a.get("rest_home") if a.get("rest_home") is not None else "-"}/{a.get("rest_away") if a.get("rest_away") is not None else "-"} d'
        odds_parts = []
        for k in SIGNS:
            o = ev["odds"].get(k)
            d = (a.get("move") or {}).get(k, 0)
            arrow = "↓" if d >= FLAG_ODDS_MOVE else "↑" if d <= -FLAG_ODDS_MOVE else " "
            odds_parts.append(f"{fmt_odds(o):>5}{arrow}")
        folk = ev.get("svenska_folket") or {}
        folk_s = "/".join(f'{folk[k]:.0f}' if folk.get(k) is not None else "-" for k in SIGNS)
        n_flags = len(a.get("flags") or [])
        print(f'  {ev["nr"]:>2} {match[:30]:30} {tab:>7}  {form:13}  {rest:>7}   {" ".join(odds_parts):28}  {folk_s:>11}   '
              f'{"!" * min(n_flags, 3)}')
    print("  Form = formbetyg 0-10 (5 = medel), hemmalagets hemmaform / bortalagets bortaform.  ↓ = oddset har sjunkit, ↑ = stigit.")

    print_coupon(draw)

    flagged = [ev for ev in draw["events"] if (ev.get("analysis") or {}).get("flags")]
    if flagged:
        print("\n  VÄRT ATT LÄSA PÅ OM:")
        for ev in flagged:
            print(f'   {ev["nr"]:>2}. {match_name(ev)}')
            for _, text in ev["analysis"]["flags"]:
                print(f"       - {text}")


# --------------------------------------------------------------------------- #
# Webbsida (en fristående HTML-fil som öppnas i webbläsaren)
# --------------------------------------------------------------------------- #
HTML_CSS = """
.rk{font-size:.78em;color:var(--muted);font-weight:400}
:root{--bg:#f7f7f5;--card:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;
--good:#1f7a4d;--good-bg:#e3f3ea;--bad:#b3261e;--bad-bg:#fbe7e5;--warn:#8a5a00;--warn-bg:#fdf1d8;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1d;--text:#ececea;--muted:#a3a39e;--line:#33332f;
--good:#6fd39c;--good-bg:#173526;--bad:#ff8a80;--bad-bg:#3a1c1a;--warn:#f3c56b;--warn-bg:#3a2e14;--accent:#8fb0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--muted);margin:0 0 18px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:900px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.lg{color:var(--muted);font-size:12px}
.chip{display:inline-block;padding:1px 7px;border-radius:999px;font-variant-numeric:tabular-nums;font-size:13px}
.pos{background:var(--good-bg);color:var(--good)}.neg{background:var(--bad-bg);color:var(--bad)}.neu{background:var(--line)}
.over{background:var(--bad-bg);color:var(--bad);border-radius:4px;padding:0 4px}
.under{background:var(--good-bg);color:var(--good);border-radius:4px;padding:0 4px}
.down{color:var(--good);font-weight:600}.up{color:var(--bad);font-weight:600}
ul.flags{margin:0;padding-left:16px;font-size:13px}ul.flags li{margin:1px 0}
.f-odds{color:var(--accent)}.f-streck{color:var(--warn)}.f-vila,.f-status{color:var(--bad)}.f-form{color:var(--good)}
.f-folj{color:var(--accent);font-weight:600}
.og{display:grid;grid-template-columns:repeat(3,auto);gap:0 12px;justify-content:start}
.oc{display:flex;flex-direction:column;align-items:flex-start}
.oc small{color:var(--muted);font-size:12px;line-height:1.2}
.oc small.xgp,small.xgp{color:var(--accent);font-size:12px}
.tip{font-weight:700;font-size:17px;letter-spacing:.04em}
td.pick{font-weight:800;font-size:20px;text-align:center;letter-spacing:.04em}
.tipv{font-weight:700;color:var(--accent);letter-spacing:.04em;margin-left:6px}
.tipk{font-weight:700;color:var(--warn);letter-spacing:.04em;margin-left:6px}
.f-varde{color:var(--good);font-weight:600}
.coupon{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 16px}
.cg{display:flex;flex-wrap:wrap;gap:6px 10px;margin:8px 0}
.ci{border:1px solid var(--line);border-radius:8px;padding:4px 8px;display:flex;align-items:baseline;gap:6px;min-width:74px}
.cn{color:var(--muted);font-size:12px}
.xgv{font-size:12px;color:var(--accent);margin-left:4px;font-variant-numeric:tabular-nums}
.f-xg{color:var(--accent)}
table.xt{min-width:0;width:auto;font-size:13px;margin:6px 0 2px}
table.xt th,table.xt td{padding:3px 10px 3px 0;border-bottom:1px solid var(--line);white-space:nowrap}
table.xt td{font-variant-numeric:tabular-nums}
.luck-pos{color:var(--bad)}.luck-neg{color:var(--good)}
.fl{display:flex;align-items:center;gap:6px;margin:2px 0;white-space:nowrap}
.fl .who{font-size:11px;color:var(--muted);width:12px}
.res{display:inline-flex;gap:2px}
.r{display:inline-block;width:18px;height:18px;border-radius:4px;font-size:11px;line-height:18px;text-align:center;font-weight:600}
.rV{background:var(--good-bg);color:var(--good)}.rO{background:var(--line);color:var(--text)}.rF{background:var(--bad-bg);color:var(--bad)}
details.gm{margin-top:4px}details.gm summary{cursor:pointer;color:var(--accent);font-size:12px}
table.gt{min-width:0;width:auto;margin-top:4px;font-size:12px}
table.gt td{padding:2px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
table.gt td.p{text-align:right;font-variant-numeric:tabular-nums}
.gt .hd td{color:var(--muted);font-weight:600;border-bottom:0;padding-top:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.card h3{margin:0 0 6px;font-size:16px}.team{margin:10px 0 0}.team b{font-weight:600}
.games{color:var(--muted);font-size:13px;margin:2px 0 0 12px}
.cur{font-weight:600;color:var(--text)}
.legend{color:var(--muted);font-size:13px;margin:10px 2px 0}
.f-skada{color:var(--bad);font-weight:600}ul.abs{margin:2px 0 0;padding-left:18px}ul.abs li.key{color:var(--bad);font-weight:600}
.f8a{font-size:12px;color:var(--bad);margin-top:2px;white-space:normal;max-width:330px}
.bld{background:var(--card);border:2px solid var(--warn);border-radius:10px;padding:12px 14px;margin:0 0 16px}
.bld h2{margin:0 0 8px;font-size:17px}
.ctl{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;margin:0 0 10px;font-size:14px}
.ctl label{display:flex;align-items:center;gap:6px;color:var(--muted)}
.ctl select,.ctl input{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
.ctl input[type=number]{width:64px}
.ctl input:disabled,.ctl select:disabled{opacity:.4}
table.bt{min-width:0;width:100%}
table.bt td{padding:4px 8px;vertical-align:middle}
.sbs{display:flex;gap:6px}
td.f8{white-space:nowrap}.f8r{display:flex;align-items:center;gap:6px;margin:1px 0}
.f8r .tn{width:116px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:12px;color:var(--muted)}
.f8c{font-size:12px;color:var(--text);margin-top:3px;white-space:normal;max-width:330px}
table.bt th{font-size:11px;padding:6px 8px;vertical-align:bottom}table.bt th .lg{text-transform:none;letter-spacing:0;font-weight:400}.f8v{margin-top:2px}.f8v b.pos{color:var(--good)}.f8v b.neg{color:var(--bad)}.f8r .chip{font-size:12px;min-width:38px;text-align:center}
.sb{min-width:62px;border:1px solid var(--line);background:transparent;color:var(--text);border-radius:8px;padding:3px 6px;cursor:pointer;line-height:1.15;font:inherit;text-align:center}
.sb b{font-size:17px}.sb small{display:block;font-size:11px;color:var(--muted)}
.sb.val small{color:var(--good);font-weight:600}
.sb.on{background:var(--warn);border-color:var(--warn);color:var(--card)}
.sb.on small{color:var(--card);opacity:.85}
.bsys{margin:4px 0 12px;font-size:14px}.bsys summary{cursor:pointer}
table.st{width:auto;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;min-width:0}
table.st th,table.st td{padding:3px 10px;text-align:right;border-bottom:1px solid var(--line)}table.st th:first-child,table.st td:first-child{text-align:left}
table.st th{font-size:11px;color:var(--muted);font-weight:600}
table.st tbody tr{cursor:pointer}table.st tbody tr:hover{background:var(--line)}
table.st tr.cur td{font-weight:700}table.st tr.dyr td{opacity:.5}table.st .best{color:var(--good);font-weight:700}
.bsum{margin:10px 0 4px;font-size:14px}.bsum b{font-variant-numeric:tabular-nums}
.bbtn{font:inherit;border:1px solid var(--accent);background:transparent;color:var(--accent);border-radius:6px;padding:4px 10px;cursor:pointer;margin:4px 8px 0 0}
.bbtn:hover{background:var(--accent);color:var(--card)}
.warnt{color:var(--bad)}

@media (max-width:640px){
 table.ov{min-width:0;width:100%}table.ov,table.ov tbody{display:block}table.ov thead{display:none}
 table.ov tr{display:block;padding:10px 12px;border-bottom:1px solid var(--line)}
 table.ov td{display:flex;gap:10px;align-items:flex-start;border:0;padding:3px 0;white-space:normal;text-align:left;min-width:0}
 table.ov td>*{min-width:0;max-width:100%;overflow-wrap:anywhere}table.ov td *{white-space:normal}
 table.ov .chip,table.ov .r,table.ov .og span{white-space:nowrap;overflow-wrap:normal}
 table.ov .fl,table.ov .og{flex-wrap:wrap}table.ov .res{flex-wrap:wrap}
 table.ov td.nr{display:none}table.ov td.mt{font-size:16px;font-weight:600;padding-bottom:6px}
 table.ov td[data-l]::before{content:attr(data-l);flex:0 0 76px;color:var(--muted);font-size:12px;font-weight:600;padding-top:2px}
 table.ov td:empty{display:none}
 table.bt{min-width:0}table.bt thead{display:none}
 table.bt tr{display:grid;grid-template-columns:22px 1fr;gap:6px 6px;padding:10px 2px;border-bottom:1px solid var(--line)}
 table.bt td{display:block;padding:0;border:0}
 table.bt td:nth-child(3),table.bt td:nth-child(4){grid-column:1/-1}
 table.bt td:nth-child(2){font-size:16px;font-weight:600}
 .sbs{justify-content:flex-start}.sb{flex:1;max-width:110px}
 .f8c{max-width:none}.tn{width:auto;min-width:96px;max-width:46%}
 table.st th:nth-child(n+5),table.st td:nth-child(n+5){display:none}
 table.st{width:100%;font-size:12px}table.st th,table.st td{padding:3px 4px;white-space:nowrap}
 table.st td:first-child,table.st th{white-space:normal}
 .wrap{border:0;border-radius:0;background:transparent;overflow-x:visible}
 .bld{border:0;padding:0;background:transparent;box-shadow:none}
 main{padding-left:12px;padding-right:12px}
 .ctl{gap:8px 12px}.ctl input[type=number]{width:64px}
 table.bt tr,table.ov tr{padding-left:0;padding-right:0}
}
@media (max-width:340px){main{padding-left:8px;padding-right:8px}table.st th,table.st td{padding:3px 3px}}
@media (max-width:560px){.sb{min-width:46px;padding:2px 3px}.sb small{font-size:10px;white-space:nowrap}.sbs{gap:4px}table.bt td{padding:4px 5px}.bld{padding:0}}
"""


def _e(s):
    import html
    return html.escape("" if s is None else str(s))


def _chip(v):
    if v is None:
        return '<span class="chip neu">-</span>'
    cls = "pos" if v >= 1 else "neg" if v <= -1 else "neu"
    return f'<span class="chip {cls}" title="Formbetyg av 10 ({grade_word(v)}), 5 = medel">{grade_s(v)}</span>'


def _form_html(f, label, current):
    if not f or not f.get("games"):
        return f'<div class="games">{label}: inga ligamatcher än</div>'
    games = " · ".join(
        f'{_e(g["opp_name"])}{"(" + num(g["opp_rank"]) + ")" if g["opp_rank"] is not None else ""} '
        f'{g["gf"]}-{g["ga"]}{" " + format(g["points"], "+d") if g["points"] is not None else ""}'
        for g in f["games"])
    pg = f'formbetyg {grade_s(f["per_game"])}' if f.get("per_game") is not None else "ej viktad"
    cls = ' class="games cur"' if current else ' class="games"'
    return f'<div{cls}>{label}{" (gäller)" if current else ""}: <b>{_e(f["form"])}</b> {pg} &nbsp; {games}</div>'


def _fx(v):
    if isinstance(v, (int, float)) and abs(v) < 0.05:
        v = 0.0
    return f"{v:.1f}".replace(".", ",")


def _form_cell(h, aw, a, xg=None):
    """Formkolumnen: snitt + färgade rutor för de senaste matcherna, och en utfällbar lista."""
    def line(who, t, key, pg, xs):
        games = ((t or {}).get(key) or {}).get("games") or []
        boxes = "".join(
            f'<span class="r r{g["res"]}" title="{g["date"][5:]} {_e(g["opp_name"])}'
            f'{" (" + num(g["opp_rank"]) + ")" if g["opp_rank"] is not None else ""} {g["gf"]}-{g["ga"]}'
            f'{" " + format(g["points"], "+d") if g["points"] is not None else ""}">{g["res"]}</span>'
            for g in games)
        xg_s = (f'<span class="xgv" title="xG för - emot per match ({xs["n"]} m)">xG {_fx(xs["xg"])}–{_fx(xs["xga"])}</span>'
                if xs else "")
        return f'<div class="fl"><span class="who">{who}</span>{_chip(pg)}<span class="res">{boxes}</span>{xg_s}</div>', games

    xg = xg or {}
    lh, gh = line("H", h, "form_home", a.get("form_home"), ((xg.get("home") or {}).get("home")))
    la, ga = line("B", aw, "form_away", a.get("form_away"), ((xg.get("away") or {}).get("away")))
    if not gh and not ga:
        return lh + la

    def rows(title, games):
        if not games:
            return f'<tr class="hd"><td colspan="5">{_e(title)}: inga matcher än</td></tr>'
        out = f'<tr class="hd"><td colspan="5">{_e(title)}</td></tr>'
        for g in games:
            rank = f' ({num(g["opp_rank"])})' if g["opp_rank"] is not None else ""
            pts = format(g["points"], "+d") if g["points"] is not None else ""
            out += (f'<tr><td>{g["date"][5:]}</td><td>{_e(g["opp_name"])}{rank}</td>'
                    f'<td>{g["gf"]}-{g["ga"]}</td><td><span class="r r{g["res"]}">{g["res"]}</span></td>'
                    f'<td class="p">{pts}</td></tr>')
        return out

    table = (rows(f'{(h or {}).get("name", "")} hemma', gh) + rows(f'{(aw or {}).get("name", "")} borta', ga))
    return (lh + la + f'<details class="gm"><summary>visa matcher</summary>'
            f'<table class="gt">{table}</table></details>')


def _pct(v):
    return "-" if v is None else f"{100 * v:.0f} %"


def _sgn(v):
    if v is None:
        return "-"
    if abs(v) < 0.05:
        return "0,0"
    return ("+" if v > 0 else "") + _fx(v)


def _luck_cls(v, limit):
    return "luck-pos" if v >= limit else "luck-neg" if v <= -limit else ""


def _prognos_cell(pg, folk):
    if not pg:
        return ""
    p = pg["p"]
    probs = " · ".join(f'{SIGNS[k]} {p[k]:.0f}' for k in SIGNS)
    extra = ""
    if pg["tips_value"] != pg["tips"]:
        extra = f'<div class="lg">med värde: <span class="tipv">{pg["tips_value"]}</span></div>'
    val = ""
    if pg["value_signs"]:
        k = pg["value_signs"][0]
        val = (f'<div class="lg" title="Vår sannolikhet {p[k]:.0f} % mot folkets {folk[k]:.0f} %">'
               f'värde {SIGNS[k]}: {pg["value"][k]:.2f}'.replace(".", ",") + '</div>')
    return f'<span class="tip">{pg["tips"]}</span><div class="lg">{probs} %</div>{extra}{val}'


def _reduced_html(draw, full_rows):
    red = draw.get("_played")
    if not red or red[1] is None:
        return ""
    n = len(draw["events"])
    ch = red[3]
    pct = lambda x, d: f"{100 * x:.{d}f}".replace(".", ",")
    rows_txt = "<br>".join(" ".join(r) for r in red[1])
    return (f'<div style="margin-top:6px"><b>Reducerat till {red[2]} rader</b> (av {full_rows}). '
            f'Chans: {n} rätt {pct(ch[n], 2)} %, {n - 1}+ {pct(ch[n - 1], 1)} %, {n - 2}+ {pct(ch[n - 2], 1)} %. '
            f'<details><summary>Visa raderna</summary><div style="font-family:monospace;line-height:1.5">{rows_txt}</div>'
            f'</details></div>')


def _absent_short(t):
    """Ordinarie spelare som saknas, kort för kupongbyggaren."""
    return [f'{p["name"]} {p.get("start_pct")} %' for p in t.get("absent") or [] if (p.get("start_pct") or 0) >= KEY_PLAYER_PCT]


def _form8(f):
    """Kompakt form för kupongbyggaren: resultat (senaste först), medelpoäng och matcherna som tooltip."""
    if not f or not f.get("games"):
        return None
    games = [f'{g["date"][5:]} {"H" if g["home"] else "B"} {g.get("opp_name") or ""} {g["gf"]}-{g["ga"]}'
             + (f' ({g["points"]:+d}p)' if g.get("points") is not None else "") for g in f["games"]]
    return {"res": f["form"], "mean": f.get("mean"), "ppg": f.get("ppg"), "games": games}


def _builder_html(draw):
    """Interaktiv kupongbyggare: välj strategi, system och reducering direkt i webbläsaren."""
    if not any((ev.get("analysis") or {}).get("prognos") for ev in draw["events"]):
        return ""
    matches = []
    for ev in draw["events"]:
        pg = (ev.get("analysis") or {}).get("prognos")
        p = pg["p"] if pg else {k: 100 / 3 for k in SIGNS}
        v = (pg or {}).get("value") or {}
        folk = ev.get("svenska_folket") or {}
        matches.append({"nr": ev["nr"], "match": f'{ev["home"]} – {ev["away"]}', "rank": [team_pos(ev, "home"), team_pos(ev, "away")],
                        "cross": bool((ev.get("stats") or {}).get("cross_league")),
                        "lg": [LEAGUE_SHORT.get(((ev.get("stats") or {}).get(sd) or {}).get("form_league") or "", "")
                               for sd in ("home", "away")],
                        "p": {SIGNS[k]: round(p[k], 2) for k in SIGNS},
                        "v": {SIGNS[k]: (round(v[k], 3) if v.get(k) is not None else None) for k in SIGNS},
                        "folk": {SIGNS[k]: folk.get(k) for k in SIGNS},
                        "form": [_form8(((ev.get("stats") or {}).get(side) or {}).get("form_all")) for side in ("home", "away")],
                        "absent": [_absent_short(((ev.get("stats") or {}).get(side) or {})) for side in ("home", "away")],
                        "venue": [_form8(((ev.get("stats") or {}).get("home") or {}).get("form_home")),
                                  _form8(((ev.get("stats") or {}).get("away") or {}).get("form_away"))]})
    name = {"stryktipset": "Stryktipset", "europatipset": "Europatipset"}.get(draw["product"], draw["product"].capitalize())
    data = {"matches": matches, "skrallMin": SKRALL_MIN_PROB, "rader": RADER, "kryss": KRYSS_VIKT, "valueMin": VALUE_MIN_RATIO, "valueProb": VALUE_MIN_PROB,
            "prognos": prognos_label(),
            "product": name, "file": f'{draw["product"]}_{close_date(draw)}_{draw["draw_number"]}',
            "maxSystem": REDUCE_MAX_SYSTEM, "keyPct": KEY_PLAYER_PCT,
            "init": {"strat": play_strategy(draw), "hel": SYSTEM.get("hel"), "halv": SYSTEM.get("halv"),
                     "garanti": SYSTEM.get("garanti"), "max": SYSTEM.get("max"), "autoSys": bool(SYSTEM.get("auto"))},
            "fund": draw.get("fund") or {}, "payTop": str(draw.get("product", "")).startswith("topptipset")}
    js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (f'<section class="bld" id="bld"><h2>Bygg din kupong</h2>'
            f'<script type="application/json" id="bld-data">{js}</script>'
            f'{BUILDER_HTML}<script>{BUILDER_JS}</script></section>')


BUILDER_HTML = """
<div class="ctl">
 <label>Strategi <select id="b-strat">
   <option value="skrall" selected>Skräll</option><option value="value">Med värde</option><option value="safe">Säkrast</option></select></label>
 <label>System <select id="b-mode"><option value="auto">Automatiskt</option><option value="fixed">Välj hel/halv</option></select></label>
 <label>Rader <input type="number" id="b-rader" min="1" max="100000"></label>
 <label>Hel <input type="number" id="b-hel" min="0" max="13" value="4"></label>
 <label>Halv <input type="number" id="b-halv" min="0" max="13" value="3"></label>
 <label title="Räkna X så här mycket troligare när kupongen fylls i. Chanserna som visas räknas alltid på riktiga sannolikheter.">Kryssvikt <select id="b-kryss">
   <option value="0">+0 %</option><option value="10">+10 %</option><option value="20">+20 %</option><option value="30">+30 %</option><option value="50">+50 %</option></select></label>
 <label>Reducering <select id="b-red"></select></label>
 <label>Max rader <input type="number" id="b-max" min="1" placeholder="alla"></label>
 <button class="bbtn" id="b-fill" title="Fyll i kupongen igen från inställningarna">Fyll i igen</button>
</div>
<details class="bsys" id="b-sysbox" open><summary><b>Systemförslag</b> <span class="lg" id="b-sysrec"></span></summary>
<div class="ctl" style="margin:6px 0"><label>Max kostnad <input type="number" id="b-budget" min="4" step="1" style="width:80px"> kr</label>
<span class="lg">Räknat på den här omgången med vald strategi. Gråa rader kostar mer än din maxkostnad. ★ = bästa systemet inom maxkostnaden. Klicka på en rad för att välja den.</span></div>
<div class="wrap"><table class="st"><thead id="b-syshead"></thead><tbody id="b-sys"></tbody></table></div>
<p class="lg" id="b-sysnote"></p></details>
<div class="wrap"><table class="bt">
<thead><tr><th>Nr</th><th>Match</th><th>Välj tecken<br><span class="lg">vår chans · folkets streck</span></th>
<th>Form, 8 senaste ligamatcherna<br><span class="lg">senaste först · formbetyg 0–10 till höger (5 = medel) · håll musen över för matcherna</span></th></tr></thead>
<tbody id="b-rows"></tbody></table></div>
<div class="bsum" id="b-sum"></div>
<div class="bsum" id="b-red-sum"></div>
<div><button class="bbtn" id="b-dl">Ladda ner rader (.txt)</button><button class="bbtn" id="b-copy">Kopiera kupongen</button>
<span class="lg" id="b-msg"></span></div>
<p class="lg"><b>Form:</b> V/O/F = vinst/oavgjort/förlust. Formbetyg 0–10: 5 = medel, 10 = vunnit allt mot topplag, 0 = förlorat allt
mot bottenlag (varje match räknas efter motståndarens plats i tabellen). Grönt = 6,3 eller mer, rött = 3,8 eller mindre. "På plan" = hemmalaget i sina
hemmamatcher och bortalaget i sina bortamatcher. Bara ligamatcher den här säsongen.</p>
<p class="lg">Klicka på ett tecken för att lägga till eller ta bort det. Små siffror: vår sannolikhet · folkets streck.
Grönt = understreckat (spelvärde). Reducering med garanti: om rätt rad finns i systemet har minst en rad så många rätt.
Filen har formatet <i>Stryktipset</i> + en rad per spelrad (E,1,X,2,...) - kontrollera mot Svenska Spels instruktioner för egna rader innan du laddar upp.</p>
"""

BUILDER_JS = r"""
(function(){
const D = JSON.parse(document.getElementById('bld-data').textContent);
const S = ['1','X','2'], N = D.matches.length;
const $ = id => document.getElementById(id);
const P = D.matches.map(m => ({'1': m.p['1']/100, 'X': m.p['X']/100, '2': m.p['2']/100}));
let sel = D.matches.map(() => []);
let reduced = null, token = 0;
const pct = (x, d) => (100*x).toFixed(d).replace('.', ',');
function adj(m){
  const b = +($('b-kryss') ? $('b-kryss').value : D.kryss) || 0;
  if (!b) return m;
  const raw = {}; let tot = 0;
  for (const k of S){ raw[k] = m.p[k] * (k === 'X' ? 1 + b/100 : 1); tot += raw[k]; }
  const p = {}, v = {};
  for (const k of S){ p[k] = 100 * raw[k] / tot; v[k] = m.v[k] != null && m.p[k] ? m.v[k] * p[k] / m.p[k] : m.v[k]; }
  return {p, v};
}
const fav = m0 => { const m = adj(m0); return S.reduce((a, b) => m.p[b] > m.p[a] ? b : a); };

function weights(strat){
  return D.matches.map(m0 => {
    const m = adj(m0), w = {};
    for (const k of S){
      const p = m.p[k], v = m.v[k];
      if (strat === 'skrall') w[k] = p >= D.skrallMin ? (v != null ? v : p/33.3) : 0;
      else if (strat === 'value') w[k] = p * Math.min(v != null ? v : 1, 2);
      else w[k] = p;
    }
    const f = fav(m0);
    if (strat === 'skrall' && w[f] <= 0) w[f] = 0.01;
    return w;
  });
}
function autoCoupon(strat, budget){
  const W = weights(strat), ch = D.matches.map(m => [fav(m)]);
  let rows = 1;
  for (;;){
    let best = null, bg = 0;
    ch.forEach((signs, i) => {
      const n = signs.length;
      if (n === 3 || rows / n * (n + 1) > budget) return;
      const s = signs.reduce((a, k) => a + W[i][k], 0);
      for (const k of S){
        if (signs.includes(k) || W[i][k] <= 0) continue;
        const g = Math.log((s + W[i][k]) / s) / Math.log((n + 1) / n);
        if (g > bg){ bg = g; best = [i, k]; }
      }
    });
    if (!best) break;
    const [i, k] = best;
    rows = rows / ch[i].length * (ch[i].length + 1);
    ch[i].push(k);
  }
  return ch;
}
function systemCoupon(strat, hel, halv){
  const W = weights(strat);
  const opts = D.matches.map((m, i) => {
    const f = fav(m), o = S.filter(k => k !== f).sort((a, b) => W[i][b] - W[i][a]);
    const lg = ks => Math.log(Math.max(ks.reduce((a, k) => a + W[i][k], 0), 1e-9));
    return [[[f], lg([f])], [[f, o[0]], lg([f, o[0]])], [[f, ...o], lg([f, ...o])]];
  });
  let dp = new Map([['0,0', [0, []]]]);
  for (const op of opts){
    const nx = new Map();
    for (const [key, [sc, path]] of dp){
      const [h, b] = key.split(',').map(Number);
      for (const [kind, dh, db] of [[0,0,0],[1,0,1],[2,1,0]]){
        const H = h + dh, B = b + db;
        if (H > hel || B > halv) continue;
        const v = sc + op[kind][1], kk = H + ',' + B;
        if (!nx.has(kk) || v > nx.get(kk)[0]) nx.set(kk, [v, path.concat(kind)]);
      }
    }
    dp = nx;
  }
  const r = dp.get(hel + ',' + halv);
  return r ? r[1].map((kind, i) => opts[i][kind][0].slice()) : null;
}
const ordered = ss => S.filter(k => ss.includes(k));
const nRows = () => sel.reduce((a, s) => a * s.length, 1);
function rowP(str){ let p = 1; for (let i = 0; i < str.length; i++) p *= P[i][str[i]]; return p; }
function allRows(){
  let rows = [''];
  for (const s of sel){ const nx = []; for (const r of rows) for (const k of ordered(s)) nx.push(r + k); rows = nx; }
  return rows;
}
function reduceRows(garanti, maxRows){
  const rows = allRows(), rp = new Map(rows.map(r => [r, rowP(r)]));
  if (!garanti){ rows.sort((a, b) => rp.get(b) - rp.get(a)); return maxRows ? rows.slice(0, maxRows) : rows; }
  const radius = N - garanti, gpos = sel.map((s, i) => s.length > 1 ? i : -1).filter(i => i >= 0);
  function ball(str){
    const out = [str], cur = str.split('');
    (function rec(start, d){
      if (d === 0) return;
      for (let pi = start; pi < gpos.length; pi++){
        const i = gpos[pi], orig = cur[i];
        for (const c of sel[i]){ if (c === orig) continue; cur[i] = c; out.push(cur.join('')); rec(pi + 1, d - 1); }
        cur[i] = orig;
      }
    })(0, radius);
    return out;
  }
  const unc = new Set(rows);
  const gain = r => { let g = 0; for (const x of ball(r)) if (unc.has(x)) g += rp.get(x); return g; };
  // binär max-heap med lat uppdatering
  const heap = rows.map(r => [gain(r), r]);
  const less = (a, b) => heap[a][0] < heap[b][0];
  const swap = (a, b) => { const t = heap[a]; heap[a] = heap[b]; heap[b] = t; };
  const down = i => { for (;;){ let l = 2*i+1, r = l+1, m = i;
      if (l < heap.length && less(m, l)) m = l; if (r < heap.length && less(m, r)) m = r;
      if (m === i) return; swap(i, m); i = m; } };
  const up = i => { while (i > 0){ const p = (i-1) >> 1; if (!less(p, i)) return; swap(i, p); i = p; } };
  for (let i = (heap.length >> 1) - 1; i >= 0; i--) down(i);
  const pop = () => { const top = heap[0], last = heap.pop(); if (heap.length){ heap[0] = last; down(0); } return top; };
  const push = x => { heap.push(x); up(heap.length - 1); };
  const chosen = [];
  while (unc.size && heap.length && (!maxRows || chosen.length < maxRows)){
    const [, r] = pop(), g = gain(r);
    if (g <= 0) continue;
    if (heap.length && g < heap[0][0] - 1e-18){ push([g, r]); continue; }
    chosen.push(r);
    for (const x of ball(r)) unc.delete(x);
  }
  return chosen;
}
function chancesFull(){
  // fördelning av antal missar: produkt av (c + (1-c) z)
  let poly = [1];
  sel.forEach((s, i) => {
    const c = s.reduce((a, k) => a + P[i][k], 0), nx = new Array(poly.length + 1).fill(0);
    poly.forEach((v, j) => { nx[j] += v * c; nx[j+1] += v * (1 - c); });
    poly = nx;
  });
  return [poly[0], poly[0] + (poly[1]||0), poly[0] + (poly[1]||0) + (poly[2]||0)];
}
function chancesRows(rows){
  const seen = new Set(), res = [];
  for (let dist = 0; dist <= 2; dist++){
    for (const r of rows){
      const cur = r.split('');
      (function rec(start, d){
        if (d === 0){ seen.add(cur.join('')); return; }
        for (let i = start; i < N; i++){
          const orig = cur[i];
          for (const c of S){ if (c === orig) continue; cur[i] = c; rec(i + 1, d - 1); }
          cur[i] = orig;
        }
      })(0, dist);
    }
    let t = 0; for (const o of seen) t += rowP(o);
    res.push(t);
  }
  return res;
}
function valueIndexFull(){
  let idx = 1;
  sel.forEach((s, i) => { idx *= s.reduce((a, k) => a + (D.matches[i].v[k] ?? 1), 0); });
  const n = nRows(); return n ? idx / n : 0;
}
function valueIndexRows(rows){
  if (!rows.length) return 0;
  let t = 0;
  for (const r of rows){ let v = 1; for (let i = 0; i < N; i++) v *= (D.matches[i].v[r[i]] ?? 1); t += v; }
  return t / rows.length;
}

function short(n){ n = String(n || ''); return n.length > 13 ? n.slice(0, 12) + '…' : n; }
const grade = x => Math.max(0, Math.min(10, (x + 4) * 1.25));
const gradeWord = x => { const g = grade(x); return g >= 7.5 ? 'toppform' : g >= 6 ? 'bra form' : g >= 4 ? 'medelform' : g >= 2.5 ? 'svag form' : 'dålig form'; };
const g1 = x => grade(x).toFixed(1).replace('.', ',');
function fmtMean(f){
  if (!f) return '–';
  return f.mean != null ? g1(f.mean) : (f.ppg != null ? f.ppg.toFixed(1).replace('.', ',') + 'p' : '–');
}
function meanCls(f){ return !f || f.mean == null ? 'neu' : f.mean >= 1 ? 'pos' : f.mean <= -1 ? 'neg' : 'neu'; }
const rk = r => r != null ? ` <span class="rk">(${r})</span>` : '';
function matchHtml(m){
  const [hn, an] = m.match.split(' – '), r = m.rank || [];
  return `${esc(hn)}${rk(r[0])} – ${esc(an)}${rk(r[1])}`;
}
function form8(f, team, pos){
  const tn = `<span class="tn" title="${esc(team)}">${esc(short(team))}${rk(pos)}</span>`;
  if (!f) return `<div class="f8r">${tn}<span class="lg">ingen form</span></div>`;
  const chips = f.res.split('').map((r, i) => `<span class="r r${r}" title="${esc(f.games[i] || '')}">${r}</span>`).join('');
  const tip = f.mean != null ? `Formbetyg ${g1(f.mean)} av 10 (${gradeWord(f.mean)}). 5 = medel. Viktat mot motståndarens tabellplats.` : 'Poäng per match (3/1/0) - tabell saknas';
  return `<div class="f8r">${tn}<span class="res">${chips}</span><span class="chip ${meanCls(f)}" title="${tip}">${fmtMean(f)}</span></div>`;
}
function venue(v){
  if (!v || (!v[0] && !v[1])) return '';
  const part = (f, label) => {
    if (!f) return `${label} –`;
    return `<span title="${esc(f.games.join('\n'))}">${label} <b class="${meanCls(f) === 'neu' ? '' : meanCls(f)}">${fmtMean(f)}</b> (${f.res.length} m)</span>`;
  };
  return `<div class="f8v lg">På plan: ${part(v[0], 'hemma')} · ${part(v[1], 'borta')}</div>`;
}
function absentLine(m){
  const [hn, an] = m.match.split(' – '), a = m.absent || [[], []];
  const parts = [[hn, a[0]], [an, a[1]]].filter(x => x[1] && x[1].length)
    .map(([n, l]) => `${short(n)} saknar ${l.length} ordinarie (${l.join(', ')})`);
  return parts.length ? `<div class="f8a" title="Startat i minst ${D.keyPct} % av ligamatcherna">⚠ ${esc(parts.join(' · '))}</div>` : '';
}
function formComment(m){
  // Kort slutsats först (fetstil), sedan varför. Returnerar HTML.
  const [h, a] = m.form || [], [hv, av] = m.venue || [];
  const [hn, an] = m.match.split(' – ').map(esc);
  if (!h && !a) return 'Ingen form att jämföra (inga ligamatcher eller ingen tabell, t.ex. landslag).';
  if (!h || !a) return 'Form saknas för ett av lagen.';
  if (h.mean == null || a.mean == null) return 'Ingen tabell att vikta mot - siffrorna är vanliga poäng per match (3/1/0).';
  if (m.cross) {
    const lg = m.lg || ['', ''], inl = i => lg[i] ? ` i ${esc(lg[i])}` : '';
    return `<b>Olika ligor.</b> ${hn} har ${gradeWord(h.mean)} (${g1(h.mean)})${inl(0)}, ${an} ${gradeWord(a.mean)} (${g1(a.mean)})${inl(1)}. ` +
      '<span class="lg">Betyget räknas mot lagets egen liga, så det går inte att jämföra rakt av - formen påverkar inte prognosen här.</span>';
  }
  const d = h.mean - a.mean;
  const lead = d > 0 ? hn : an, other = d > 0 ? an : hn;
  const pair = (x, y) => `${g1(x)} mot ${g1(y)}`;
  let verdict, why;
  if (Math.abs(d) < 0.5) { verdict = 'Jämn form.'; why = `${hn} ${pair(h.mean, a.mean)} ${an}`; }
  else { const wl = gradeWord(Math.max(h.mean, a.mean)), wo = gradeWord(Math.min(h.mean, a.mean));
         verdict = `${Math.abs(d) < 1.5 ? 'Liten' : 'Klar'} fördel ${lead}.`;
         why = (wl === wo ? `Båda har ${wl} - ${lead} något bättre` : `${lead} har ${wl}, ${other} ${wo}`) +
               ` (${pair(Math.max(h.mean, a.mean), Math.min(h.mean, a.mean))})`; }
  let t = `<b>${verdict}</b> ${why}.`;
  if (hv && av && hv.mean != null && av.mean != null) {
    const dv = hv.mean - av.mean, vteam = dv > 0 ? hn : an;
    const vs = `${hn} ${g1(hv.mean)} hemma, ${an} ${g1(av.mean)} borta`;
    if (Math.abs(dv) < 1) t += Math.abs(d) >= 0.5 ? ` Hemma/borta är jämnt (${vs}).` : '';
    else if (Math.abs(d) < 0.5) t += ` Men hemma/borta talar för ${vteam} (${vs}).`;
    else if (Math.sign(dv) === Math.sign(d)) t += ` Hemma/borta talar också för ${lead} (${vs}).`;
    else t += ` Men hemma/borta talar för ${vteam} (${vs}) - osäkert.`;
    if (Math.min(hv.res.length, av.res.length) < 3 && Math.abs(dv) >= 1) t += ' <span class="lg">Få hemma/borta-matcher än.</span>';
  }
  if (Math.min(h.res.length, a.res.length) < 4) t += ' <span class="lg">Få matcher än - lita mindre på formen.</span>';
  return t;
}
function renderRows(){
  $('b-rows').innerHTML = D.matches.map((m, i) => {
    const btns = S.map(k => {
      const on = sel[i].includes(k) ? ' on' : '', val = (m.v[k] ?? 0) >= D.valueMin && m.p[k] >= D.valueProb ? ' val' : '';
      const f = m.folk[k] != null ? Math.round(m.folk[k]) : '-';
      return `<button class="sb${on}${val}" data-i="${i}" data-k="${k}"><b>${k}</b><small>${Math.round(m.p[k])} · ${f}</small></button>`;
    }).join('');
    return `<tr><td class="num lg">${m.nr}</td><td>${matchHtml(m)}</td><td><div class="sbs">${btns}</div></td>` +
      `<td class="f8">${form8(m.form && m.form[0], m.match.split(' – ')[0], (m.rank || [])[0])}${form8(m.form && m.form[1], m.match.split(' – ')[1], (m.rank || [])[1])}` +
      `${venue(m.venue)}<div class="f8c">${formComment(m)}</div>${absentLine(m)}</td></tr>`;
  }).join('');
}
function counts(){
  let h = 0, b = 0, sp = 0, empty = 0;
  sel.forEach(s => { if (s.length === 3) h++; else if (s.length === 2) b++; else if (s.length === 1) sp++; else empty++; });
  return {h, b, sp, empty};
}
function summary(){
  const c = counts(), n = nRows();
  // Rutorna överst visar alltid kupongen som den ser ut just nu (även efter egna klick)
  $('b-hel').value = c.h; $('b-halv').value = c.b; $('b-rader').value = n;
  if (c.empty){ $('b-sum').innerHTML = `<span class="warnt">${c.empty} match(er) saknar tecken.</span>`; $('b-red-sum').textContent = ''; reduced = null; return; }
  const [p13, p12, p11] = chancesFull();
  $('b-sum').innerHTML = `<b>${c.h}</b> hel · <b>${c.b}</b> halv · <b>${c.sp}</b> spik = <b>${n}</b> rader. ` +
    `Chans ${N} rätt <b>${pct(p13, 2)} %</b>, ${N-1}+ ${pct(p12, 1)} %, ${N-2}+ ${pct(p11, 1)} %. ` +
    `Värdekvot per rad <b>${valueIndexFull().toFixed(2).replace('.', ',')}</b>.`;
  runReduce();
}
let redKey = '';
function runReduce(){
  const g = +$('b-red').value || 0, mx = +$('b-max').value || 0, n = nRows();
  const key = g + '|' + mx + '|' + sel.map(s => ordered(s).join('')).join(',');
  if (key === redKey && reduced) return;          // inget har ändrats
  const my = ++token;
  redKey = key; reduced = null;
  if (!g && !mx){ $('b-red-sum').textContent = ''; return; }
  if (n > D.maxSystem){ $('b-red-sum').innerHTML = `<span class="warnt">${n} rader är för stort att reducera (max ${D.maxSystem}).</span>`; return; }
  $('b-red-sum').textContent = 'Reducerar ...';
  setTimeout(() => {
    if (my !== token) return;
    const rows = reduceRows(g, mx);
    if (my !== token) return;
    reduced = rows;
    const [p13, p12, p11] = chancesRows(rows);
    const how = g ? `garanti ${g} rätt${mx && rows.length >= mx ? ' (ofullständig p.g.a. max rader)' : ''}` : 'de troligaste raderna';
    $('b-red-sum').innerHTML = `Reducerat till <b>${rows.length}</b> rader (av ${n}), ${how}. ` +
      `Chans ${N} rätt <b>${pct(p13, 2)} %</b>, ${N-1}+ ${pct(p12, 1)} %, ${N-2}+ ${pct(p11, 1)} %. ` +
      `Värdekvot per rad ${valueIndexRows(rows).toFixed(2).replace('.', ',')}.`;
  }, 20);
}
const esc = t => String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
function couponStats(c){
  let rows = 1, cover = 1, vi = 1;
  c.forEach((s, i) => { rows *= s.length; cover *= s.reduce((a, k) => a + P[i][k], 0);
                        vi *= s.reduce((a, k) => a + (D.matches[i].v[k] ?? 1), 0); });
  return {rows, cover, vi: rows ? vi / rows : 0};
}
function renderTop(fixed, h, b, bud){
  const box = document.getElementById('topbox'); if (!box) return;
  const mk = st => (fixed ? systemCoupon(st, h, b) : autoCoupon(st, bud)).map(ordered);
  const C = {safe: mk('safe'), value: mk('value'), skrall: mk('skrall')};
  const cur = $('b-strat').value, js = c => c.join('');
  const items = D.matches.map((m, i) => {
    const sf = js(C.safe[i]), v = js(C.value[i]), k = js(C.skrall[i]);
    return `<div class="ci" title="${esc(m.match)}"><span class="cn">${m.nr}</span><span class="tip">${sf}</span>` +
      (v !== sf ? `<span class="tipv">${v}</span>` : '') + (k !== sf ? `<span class="tipk">${k}</span>` : '') + '</div>';
  }).join('');
  const f2 = x => x.toFixed(2).replace('.', ','), f1 = x => (100*x).toFixed(1).replace('.', ',');
  const part = (key, name, cls, note) => {
    const st = couponStats(C[key]), mark = key === cur ? ' <u>(vald i byggaren)</u>' : '';
    return `<b${cls ? ` class="${cls}" style="margin:0"` : ''}>${name}</b> ${note}: ${st.rows} rader, chans för ${N} rätt ${f1(st.cover)} %, värdekvot ${f2(st.vi)}${mark}. `;
  };
  const kv = +$('b-kryss').value || 0;
  const label = (fixed ? `${h} hel + ${b} halv = ${3**h * 2**b} rader` : `upp till ${bud} rader`) + (kv ? `, kryssvikt +${kv} %` : '');
  box.innerHTML = `<div class="ch"><b>Kupongförslag, ${label}</b> <span class="lg">${esc(D.prognos)}</span></div>` +
    `<div class="cg">${items}</div><div class="lg">` +
    part('safe', 'Säkrast', '', '(svart)') + part('value', 'Med värde', 'tipv', '(där det skiljer)') +
    part('skrall', 'Skräll', 'tipk', '(där det skiljer)') +
    '<br>Värdekvot = förväntad utdelning per rad jämfört med ett rättvist spel. Skräll satsar på de veckor som betalar: ' +
    'färre rätt, men när den sitter är utdelningen högre. Förslagen följer systemvalet i "Bygg din kupong".</div>';
}
function coverDist(c){
  // sannolikhet för exakt k rätt (bästa raden) givet hur stor del av varje match kupongen täcker
  let d = [1];
  c.forEach((s, i) => {
    const q = s.reduce((a, k) => a + P[i][k], 0), nd = new Array(d.length + 1).fill(0);
    d.forEach((v, k) => { nd[k] += v * (1 - q); nd[k + 1] += v * q; });
    d = nd;
  });
  return k => d.slice(k).reduce((a, b) => a + b, 0);
}
let lastRec = null;
function sysSuggest(){
  const strat = $('b-strat').value, bud = Math.max(1, +$('b-budget').value || 0);
  // visa ett spann runt standardsystemet (även dyrare än budgeten, gråade) så man ser vad mer pengar ger
  const base = D.init.hel != null ? 3**D.init.hel * 2**D.init.halv : D.rader;
  const minRows = Math.max(4, Math.floor(Math.min(bud, base) * 0.33));
  const maxRows = Math.max(bud, Math.round(base * (N <= 8 ? 3 : 1.5))), list = [];
  for (let h = 0; h <= N; h++) for (let b = 0; h + b <= N; b++){
    const rows = 3**h * 2**b;
    if (rows > maxRows || rows < minRows) continue;
    const at = coverDist(systemCoupon(strat, h, b));
    list.push({h, b, rows, c: [0, 1, 2, 3].map(e => at(N - e))});
  }
  if (!list.length){ $('b-sys').innerHTML = '<tr><td colspan="7">Inget system ryms i budgeten.</td></tr>'; $('b-sysrec').textContent = ''; return; }
  list.sort((a, b) => a.rows - b.rows || a.h - b.h);
  // förslag inom budgeten: Topptipset betalar bara alla rätt -> högst chans för alla rätt;
  // Stryk/Europa -> högst chans för N-1+ (12+, där pengarna finns). Lika -> billigast.
  const key = D.payTop ? 0 : 1, inb = list.filter(x => x.rows <= bud);
  const pool = inb.length ? inb : list.slice(0, 1);
  const rec = pool.reduce((a, b) => b.c[key] > a.c[key] + 1e-12 ? b : a);
  const top = pool.reduce((a, b) => b.c[0] > a.c[0] + 1e-12 ? b : a);
  const per = x => x.c[key] / x.rows;
  lastRec = rec;
  const curH = +$('b-hel').value || 0, curB = +$('b-halv').value || 0, fixed = $('b-mode').value === 'fixed';
  const p = (x, d) => (100*x).toFixed(d).replace('.', ',');
  $('b-syshead').innerHTML = `<tr><th>System</th><th>Rader = kr</th><th>${N} rätt</th><th>${N-1}+</th><th>${N-2}+</th><th>${N-3}+</th><th>${D.payTop ? N : (N-1) + '+'} per 100 kr</th></tr>`;
  $('b-sys').innerHTML = list.map(x => {
    const tags = (x === rec ? ' <span class="best" title="Högst chans för ' + (D.payTop ? N : (N-1) + '+') + ' rätt inom budgeten">★ förslag</span>' : '') +
                 (x === top && x !== rec ? ' <span class="best" title="Högst chans för alla rätt inom budgeten">▲ flest ' + N + ' rätt</span>' : '');
    const cls = (fixed && x.h === curH && x.b === curB ? 'cur ' : '') + (x.rows > bud ? 'dyr' : '');
    return `<tr data-h="${x.h}" data-b="${x.b}" class="${cls}" title="${x.rows > bud ? 'Över din maxkostnad' : ''}">` +
      `<td>${x.h}-${x.b}${tags}</td><td>${x.rows}</td><td>${p(x.c[0], 2)} %</td><td>${p(x.c[1], 1)} %</td>` +
      `<td>${p(x.c[2], 1)} %</td><td>${p(x.c[3], 1)} %</td><td>${p(100 * per(x), 1)} %</td></tr>`;
  }).join('');
  $('b-sysrec').textContent = `förslag: ${rec.h}-${rec.b} (${rec.rows} kr)` + (top !== rec ? `, eller ${top.h}-${top.b} (${top.rows} kr) för högst chans på ${N} rätt` : '');
  const ex = P.reduce((a, m) => a + m.X, 0), open = D.matches.filter(m => Math.max(m.p['1'], m.p['2'], m.p['X']) < 45).length;
  const f = D.fund || {}, xtra = (f.extra || 0) + (f.rollover || 0);
  const kr = v => Math.round(v).toLocaleString('sv-SE');
  const fundTxt = xtra > 0 ? `<b class="best">Extrapengar i potten: ${kr(xtra)} kr</b>` +
    (f.extra && f.rollover ? ` (${kr(f.extra)} extra + ${kr(f.rollover)} jackpot)` : '') +
    ` - de går till ${D.payTop ? 'alla rätt' : 'högsta vinstgruppen'}, så ett större system kan vara värt det den här gången. Höj maxkostnaden för att se förslaget. ` : '';
  $('b-sysnote').innerHTML = fundTxt + `Förväntat antal kryss: <b>${ex.toFixed(1).replace('.', ',')}</b> av ${N}. ` +
    `Öppna matcher (inget tecken över 45 %): <b>${open}</b>. Krysset är sällan det troligaste tecknet i en match, ` +
    `så halvgarderingar blir oftast 1-2 - helgarderingar fångar krysset där det än dyker upp. ` +
    `Chanserna gäller bästa raden och bygger på prognosen, inte på utdelning.`;
}
function fill(){
  const strat = $('b-strat').value, fixed = $('b-mode').value === 'fixed';
  $('b-rader').disabled = fixed; $('b-hel').disabled = !fixed; $('b-halv').disabled = !fixed;
  let c;
  if (fixed){
    const h = +$('b-hel').value || 0, b = +$('b-halv').value || 0;
    if (h + b > N){ $('b-msg').textContent = `${h} hel + ${b} halv är fler än ${N} matcher.`; return; }
    c = systemCoupon(strat, h, b);
  } else c = autoCoupon(strat, Math.max(1, +$('b-rader').value || D.rader));
  $('b-msg').textContent = '';
  renderTop(fixed, +$('b-hel').value || 0, +$('b-halv').value || 0, Math.max(1, +$('b-rader').value || D.rader));
  sel = c.map(ordered);
  renderRows(); summary(); sysSuggest();
}
function download(){
  if (counts().empty) return;
  const n = nRows();
  const g = +$('b-red').value || 0, mx = +$('b-max').value || 0;
  if (n > D.maxSystem && !(reduced && (g || mx))){ $('b-msg').textContent = 'För många rader för en fil.'; return; }
  if ((g || mx) && !reduced){ token++; reduced = reduceRows(g, mx); runReduce(); }
  const rows = (g || mx) ? reduced : allRows();
  const txt = [D.product].concat(rows.map(r => 'E,' + r.split('').join(','))).join('\n') + '\n';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([txt], {type: 'text/plain'}));
  a.download = `${D.file}_rader.txt`; document.body.appendChild(a); a.click(); a.remove();
  $('b-msg').textContent = `${rows.length} rader sparade.`;
}
function copy(){
  const txt = D.matches.map((m, i) => `${m.nr}:${ordered(sel[i]).join('')}`).join('  ');
  const done = () => $('b-msg').textContent = 'Kopierat: ' + txt;
  if (navigator.clipboard) navigator.clipboard.writeText(txt).then(done, () => { $('b-msg').textContent = txt; });
  else $('b-msg').textContent = txt;
}

// garanti-val beror på antal matcher
$('b-red').innerHTML = '<option value="">Ingen</option>' +
  [1, 2, 3].map(e => `<option value="${N-e}">Garanti ${N-e} rätt</option>`).join('');
$('b-rader').value = D.rader;
if (D.kryss){ const o = [...$('b-kryss').options].find(o => +o.value === D.kryss);
  if (!o) $('b-kryss').insertAdjacentHTML('beforeend', `<option value="${D.kryss}">+${D.kryss} %</option>`);
  $('b-kryss').value = String(D.kryss); }
if (D.init.hel != null){ $('b-mode').value = 'fixed'; $('b-hel').value = D.init.hel; $('b-halv').value = D.init.halv; }
if (D.init.garanti) $('b-red').value = D.init.garanti;
if (D.init.max) $('b-max').value = D.init.max;
if (D.init.strat) $('b-strat').value = D.init.strat;
$('b-budget').value = D.init.hel != null ? 3**D.init.hel * 2**D.init.halv : D.rader;
$('b-budget').addEventListener('change', sysSuggest);
if (D.init.autoSys){
  // välj systemförslaget direkt (samma kostnad som standardsystemet eller billigare)
  sysSuggest();
  if (lastRec){ $('b-mode').value = 'fixed'; $('b-hel').value = lastRec.h; $('b-halv').value = lastRec.b; }
}
$('b-sys').addEventListener('click', e => {
  const tr = e.target.closest('tr[data-h]'); if (!tr) return;
  $('b-mode').value = 'fixed'; $('b-hel').value = tr.dataset.h; $('b-halv').value = tr.dataset.b; fill();
});

$('b-rows').addEventListener('click', e => {
  const b = e.target.closest('.sb'); if (!b) return;
  const i = +b.dataset.i, k = b.dataset.k;
  sel[i] = sel[i].includes(k) ? sel[i].filter(x => x !== k) : ordered(sel[i].concat(k));
  renderRows(); summary();
});
['b-strat', 'b-mode', 'b-rader', 'b-hel', 'b-halv', 'b-kryss'].forEach(id => $(id).addEventListener('change', fill));
['b-red', 'b-max'].forEach(id => $(id).addEventListener('change', runReduce));
$('b-fill').addEventListener('click', fill);
$('b-dl').addEventListener('click', download);
$('b-copy').addEventListener('click', copy);
fill();
})();
"""


def _coupon_html(draw):
    if not any((ev.get("analysis") or {}).get("prognos") for ev in draw["events"]):
        return ""
    safe, rows_s, cov_s = build_coupon(draw)
    val, rows_v, cov_v = build_coupon(draw, with_value=True)
    skr, rows_k, cov_k = build_coupon(draw, skrall=True)
    items = []
    for ev in draw["events"]:
        nr = ev["nr"]
        v = val[nr] != safe[nr]
        k = skr[nr] != safe[nr]
        items.append(f'<div class="ci" title="{_e(match_name(ev, EN_SEP))}"><span class="cn">{nr}</span>'
                     f'<span class="tip">{safe[nr]}</span>'
                     f'{"<span class=tipv>" + val[nr] + "</span>" if v else ""}'
                     f'{"<span class=tipk>" + skr[nr] + "</span>" if k else ""}</div>')
    pis = [payout_index(draw, c) for c in (safe, val, skr)]
    f2 = lambda x: f"{x:.2f}".replace(".", ",")
    n = len(draw["events"])
    return (f'<div class="coupon" id="topbox"><div class="ch"><b>Kupongförslag, {_e(system_label())}</b> '
            f'<span class="lg">{_e(prognos_label())}</span></div>'
            f'<div class="cg">{"".join(items)}</div>'
            f'<div class="lg"><b>Säkrast</b> (svart): {rows_s} rader, chans för {n} rätt {100 * cov_s:.1f} %, värdekvot {f2(pis[0])}. '
            f'<b class="tipv" style="margin:0">Med värde</b> (där det skiljer): {rows_v} rader, {100 * cov_v:.1f} %, värdekvot {f2(pis[1])}. '
            f'<b class="tipk" style="margin:0">Skräll</b> (där det skiljer): {rows_k} rader, {100 * cov_k:.1f} %, värdekvot {f2(pis[2])}.<br>'
            f'Värdekvot = förväntad utdelning per rad jämfört med ett rättvist spel. Skräll satsar på de veckor som betalar: '
            f'färre rätt, men när den sitter är utdelningen högre.</div></div>')


def _absent_html(t):
    ab = t.get("absent") or []
    if not ab:
        return ""
    items = []
    for p in ab:
        pct = p.get("start_pct")
        imp = f'startat {p.get("starts", 0)} av {p.get("games", 0)} ({pct} %)' if pct is not None else "okänt hur ofta han startat"
        cls = " class=key" if (pct or 0) >= KEY_PLAYER_PCT else ""
        why = absent_why(p)
        items.append(f'<li{cls}>{_e(p["name"])}{" (" + _e(p["pos"]) + ")" if p.get("pos") else ""}: '
                     f'{_e(why or p.get("status") or "")} · {imp}'
                     f'{" · åter " + _e(str(p["return"])[:10]) if p.get("return") else ""}</li>')
    return f'<div class="games">Borta/osäkra:<ul class="abs">{"".join(items)}</ul></div>'


def _xrank_html(xg):
    th, ta = (xg or {}).get("table_home"), (xg or {}).get("table_away")
    if not th or not ta:
        return ""
    return (f'<small class="xgp" style="display:block" title="Placering i xG-tabellen (sorterad på xPts)">'
            f'xT {th["xrank_all"]} – {ta["xrank_all"]}</small>')


def _xg_table(ev):
    xg = ev.get("xg") or {}
    if not xg.get("model"):
        return f'<p class="lg">{_e(xg["error"])}</p>' if xg.get("error") else ""
    st = ev.get("stats") or {}
    blocks = []
    for side, venue, venue_label in (("home", "home", "hemma"), ("away", "away", "borta")):
        b = xg.get(side) or {}
        name = (st.get(side) or {}).get("name") or ev.get(side)
        blocks += [(f"{name} – alla", b.get("all")), (f"{name} – {venue_label}", b.get(venue)),
                   (f"{name} – senaste {XG_RECENT}", b.get("recent"))]
    blocks = [(lbl, s) for lbl, s in blocks if s]

    # 1) Expected mot faktiskt
    r1 = ""
    for label, s in blocks:
        r1 += (f'<tr><td>{_e(label)}</td><td>{s["n"]}</td><td>{_fx(s["xg"])}</td><td>{_fx(s["xga"])}</td>'
               f'<td>{_sgn(s["xgd"])}</td><td>{_fx(s["gf"])}–{_fx(s["ga"])}</td>'
               f'<td class="{_luck_cls(s["finishing"], 2)}">{_sgn(s["finishing"])}</td>'
               f'<td class="{_luck_cls(s["keeping"], 2)}">{_sgn(s["keeping"])}</td>'
               f'<td>{_fx(s["xpts"])}</td><td class="{_luck_cls(s["pts"] - s["xpts"], FLAG_LUCK)}">{_fx(s["pts"])}</td>'
               f'<td>{_fx(s["xw"])}–{_fx(s["xd"])}–{_fx(s["xl"])}</td><td>{s["w"]}–{s["d"]}–{s["l"]}</td></tr>')
    t1 = ('<table class="xt"><tr><th>Expected mot faktiskt</th><th>M</th><th>xG</th><th>xGA</th><th>xGD</th>'
          '<th>Mål</th><th title="Gjorda mål minus xG, totalt">Avslut ±</th>'
          '<th title="xGA minus insläppta mål, totalt">Försvar ±</th><th>xPts</th><th>Poäng</th>'
          '<th>xV–xO–xF</th><th>V–O–F</th></tr>' + r1 + '</table>')

    # 2) Avslut
    r2 = ""
    for label, s in blocks:
        f = lambda v: "-" if v is None else _fx(v)
        f2 = lambda v: "-" if v is None else f"{v:.2f}".replace(".", ",")
        r2 += (f'<tr><td>{_e(label)}</td><td>{f(s["shots"])}–{f(s["shots_a"])}</td><td>{f(s["sot"])}–{f(s["sot_a"])}</td>'
               f'<td>{_pct(s["sot_pct"])}</td><td>{f2(s["xg_per_shot"])}</td><td>{f2(s["xga_per_shot"])}</td>'
               f'<td>{_pct(s["conversion"])}</td><td>{f(s["corners"])}–{f(s["corners_a"])}</td></tr>')
    t2 = ('<table class="xt"><tr><th>Avslut per match</th><th>Skott</th><th>På mål</th><th>Träff %</th>'
          '<th title="Chansernas kvalitet">xG/skott</th><th title="Motståndarnas chanskvalitet">xGA/skott</th>'
          '<th>Mål/skott</th><th>Hörnor</th></tr>' + r2 + '</table>')

    # 3) Målmönster
    r3 = ""
    for label, s in blocks:
        r3 += (f'<tr><td>{_e(label)}</td><td>{s["cs"]} / {_fx(s["xcs"])}</td>'
               f'<td>{_pct(s["btts"])} / {_pct(s["xbtts"])}</td><td>{_pct(s["o25"])} / {_pct(s["xo25"])}</td></tr>')
    t3 = ('<table class="xt"><tr><th>Målmönster (faktiskt / förväntat)</th><th>Hållna nollor</th>'
          '<th>Båda gör mål</th><th>Över 2,5 mål</th></tr>' + r3 + '</table>')

    # Tabellplaceringar
    tabs = []
    for side in ("home", "away"):
        tb = xg.get(f"table_{side}") or {}
        name = (st.get(side) or {}).get("name") or ev.get(side)
        if tb:
            tabs.append(f'{_e(name)}: tabell {tb["rank_all"]} · <b>xG-tabell {tb["xrank_all"]}</b> '
                        f'(hemma {tb["rank_home"]}/x{tb["xrank_home"]}, borta {tb["rank_away"]}/x{tb["xrank_away"]})')

    m, me = xg["model"], xg.get("model_extra") or {}
    mp, xpm = xg.get("model_xpts"), xg.get("xpts_match") or (0, 0)
    lam = xg.get("lambda") or (0, 0)
    tops = ", ".join(f'{s} ({p:.0f} %)' for s, p in me.get("top_scores", []))
    return (f'<div class="lg">{" &nbsp;|&nbsp; ".join(tabs)}</div>{t1}{t2}{t3}'
            + (f'<div class="lg"><b>xPts-prognos:</b> 1 {mp["one"]:.0f} % · X {mp["x"]:.0f} % · 2 {mp["two"]:.0f} % · '
               f'förväntade xPts i matchen {_fx(xpm[0])}–{_fx(xpm[1])}</div>' if mp else '') +
            f'<div class="lg"><b>xG-prognos:</b> 1 {m["one"]:.0f} % · X {m["x"]:.0f} % · 2 {m["two"]:.0f} % · '
            f'över 2,5 mål {me.get("over25", 0):.0f} % · båda gör mål {me.get("btts", 0):.0f} % · '
            f'förväntade mål {_fx(lam[0])}–{_fx(lam[1])} · troligast {tops}.<br>'
            f'Rött = bättre utfall än chanserna motiverar (tur?), grönt = sämre (otur?). '
            f'Avslut ± och Försvar ± är totalt över perioden.</div>')


def write_html(draw, path):
    close = draw.get("close_time") or ""
    try:
        close = datetime.fromisoformat(close.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    rows, cards = [], []
    for ev in draw["events"]:
        a = ev.get("analysis") or analyse_event(ev)
        st = ev.get("stats") or {}
        h, aw = st.get("home") or {}, st.get("away") or {}
        folk = ev.get("svenska_folket") or {}
        odds_cells, folk_cells = [], []
        for k in SIGNS:
            d = (a.get("move") or {}).get(k, 0)
            arrow = ' <span class="down">↓</span>' if d >= FLAG_ODDS_MOVE else ' <span class="up">↑</span>' if d <= -FLAG_ODDS_MOVE else ""
            start = (ev.get("start_odds") or {}).get(k)
            title = f' title="Vid öppning {fmt_odds(start)}"' if start and start != ev["odds"].get(k) else ""
            pc = (a.get("p_now") or {}).get(k)
            pc_s = f'{pc:.0f} %' if pc is not None else ""
            xm = (a.get("xg_model") or {}).get(k)
            xg_s = f'<small class="xgp" title="xG-modellens sannolikhet">xG {xm:.0f} %</small>' if xm is not None else ""
            xp = (a.get("xpts_model") or {}).get(k)
            if xp is not None:
                xg_s = f'<small class="xgp" title="xPts-modellens sannolikhet">xP {xp:.0f} %</small>' + xg_s
            odds_cells.append(f'<span class="oc"{title}>{fmt_odds(ev["odds"].get(k))}{arrow}'
                              f'<small>{pc_s}</small>{xg_s}</span>')
            sd = (a.get("streck") or {}).get(k)
            cls = "over" if sd is not None and sd >= FLAG_STRECK else "under" if sd is not None and sd <= -FLAG_STRECK else ""
            val = f'{folk[k]:.0f}' if folk.get(k) is not None else "-"
            folk_cells.append(f'<span class="{cls}">{val}</span>' if cls else val)
        rank = lambda t: num(t.get("rank")) if t.get("rank") is not None else "-"
        rest = lambda v: "-" if v is None else f"{v} d"
        flags = "".join(f'<li class="f-{kind}">{_e(text)}</li>' for kind, text in a.get("flags") or [])
        rows.append(
            f'<tr><td class="num nr">{ev["nr"]}</td>'
            f'<td class="mt"><a href="#m{ev["nr"]}">{match_html(ev)}</a><div class="lg">{_e(ev.get("league"))}</div></td>'
            f'<td class="num" data-l="Tabell">{rank(h)} – {rank(aw)}{_xrank_html(ev.get("xg"))}</td>'
            f'<td data-l="Form">{_form_cell(h, aw, a, ev.get("xg"))}</td>'
            f'<td class="num" data-l="Vila">{rest(a.get("rest_home"))} / {rest(a.get("rest_away"))}</td>'
            f'<td class="num" data-l="Odds"><div class="og">{"".join(odds_cells)}</div></td>'
            f'<td class="num" data-l="Folket">{" / ".join(folk_cells)}</td>'
            f'<td class="pick" data-l="Tips">{(a.get("prognos") or {}).get("pick", "")}</td>'
            f'<td data-l="Prognos">{_prognos_cell(a.get("prognos"), folk)}</td>'
            f'<td{" data-l=Noteringar" if flags else ""}>{"<ul class=flags>" + flags + "</ul>" if flags else ""}</td></tr>')

        teams_html = ""
        for side, t in (("home", h), ("away", aw)):
            if not t:
                continue
            rec = t.get("record") or {}
            info = []
            if t.get("rank") is not None:
                info.append(f'{num(t["rank"])}:a i {"gruppen" if (t.get("group_size") or 99) < 10 else "tabellen"}')
            if rec.get("played"):
                info.append(f'{rec["points"]}p på {rec["played"]} m ({rec["w"]}V {rec["d"]}O {rec["l"]}F)')
            r = t.get("rest")
            if r:
                info.append(f'vila {r["days"]} dagar (senast {_e(r["competition"])} mot {_e(r["opponent"])} {r["date"]})')
            teams_html += f'<div class="team"><b>{_e(t.get("name"))}</b> <span class="lg">{_e(", ".join(info))}</span>'
            teams_html += _absent_html(t)
            teams_html += _form_html(t.get("form_home"), "Hemma", side == "home")
            teams_html += _form_html(t.get("form_away"), "Borta", side == "away")
            if not (t.get("form_home") or {}).get("games") and not (t.get("form_away") or {}).get("games") and t.get("last5"):
                last = " · ".join(f'{g["date"][5:]} {"H" if g["home"] else "B"} {_e(g["opponent"])} {_e(g["score"])} ({g["result"]})'
                                  for g in sorted(t["last5"], key=lambda g: g["date"], reverse=True))
                teams_html += f'<div class="games">Senaste matcher (alla tävlingar): {last}</div>'
            teams_html += "</div>"
        err = f'<p class="lg">{_e(st.get("error"))}</p>' if st.get("error") else ""
        cards.append(f'<div class="card" id="m{ev["nr"]}"><h3>{ev["nr"]}. {match_html(ev)} '
                     f'<span class="lg">{_e(ev.get("league"))}</span></h3>'
                     f'{"<ul class=flags>" + flags + "</ul>" if flags else ""}{err}{_xg_table(ev)}{teams_html}</div>')

    title = f'{draw.get("product_name") or draw["product"].capitalize()} {draw["draw_number"]}'
    page = f"""<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)}</title>
<style>{HTML_CSS}</style></head><body><main>
<h1>{_e(title)}</h1>
<p class="sub">{_e(draw.get("comment") or "")} · spelstopp {_e(close)} · uppdaterad {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
{_builder_html(draw)}
<div class="wrap"><table class="ov">
<thead><tr><th>Nr</th><th>Match</th><th>Tabell</th><th>Form hem / bort</th><th>Vila</th>
<th>Odds 1 · X · 2</th><th>Folket 1/X/2</th><th title="Mest troliga utfallet. 1X2 = för jämnt för att peka ut ett tecken">Tips</th><th>Prognos</th><th>Värt att läsa på om</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p class="legend">Form: H = hemmalagets senaste {FORM_GAMES} hemmamatcher, B = bortalagets senaste {FORM_GAMES} bortamatcher, senaste först.
Siffran är snittpoäng viktad mot motståndarens tabellplats (äldre matcher räknas mindre). Håll musen över en ruta eller tryck "visa matcher".
↓ = oddset har sjunkit sedan omgången öppnade (håll musen över oddset för startvärdet).
Rött streck = överstreckat mot oddsen, grönt = understreckat.
xG (blått) = expected goals från football-data.co.uk: i formkolumnen skapade–insläppta chanser per match på den planen,
under oddsen xG-modellens egen sannolikhet för 1/X/2. xPts = poäng som chanserna "borde" ha gett.</p>
<h2>Detaljer</h2>{"".join(cards)}
</main></body></html>"""
    Path(path).write_text(page, "utf-8")
    return path


# --------------------------------------------------------------------------- #
# Utskrift
# --------------------------------------------------------------------------- #
def fmt_triple(t, suffix=""):
    if not any(v is not None for v in t.values()):
        return "-"
    return "/".join("-" if t[k] is None else f"{t[k]:g}{suffix}" for k in ("one", "x", "two"))


def print_draw(draw):
    close = draw["close_time"] or ""
    try:
        close = datetime.fromisoformat(close.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    print(f'\n=== {(draw.get("product_name") or draw["product"]).upper()} omgång {draw["draw_number"]} - spelstopp {close} ===')
    if draw["comment"]:
        print(f'    {draw["comment"]}')
    for ev in draw["events"]:
        analyse_event(ev)
    try:
        draw["_played"] = play_coupon(draw)
    except ValueError as e:
        print(f"\n  OBS: systemet gick inte att använda: {e}. Kör med automatisk kupong ({RADER} rader).")
        SYSTEM.clear()
        draw["_played"] = play_coupon(draw)
    print_summary(draw)

    print("\n  DETALJER")
    print(f"    Form: senaste matchen först. Formbetyg 0-10 (5 = medel) = poäng viktade mot motståndarens")
    print(f"    tabellplacering, där äldre matcher räknas mindre (faktor {FORM_DECAY}). <- = gäller i matchen.")
    for ev in draw["events"]:
        print(f'\n{ev["nr"]:>2}. {match_name(ev)}  [{ev["league"] or ""}]')
        start = ev.get("start_odds") or {}
        moved = any(start.get(k) and start.get(k) != ev["odds"].get(k) for k in SIGNS)
        pn = (ev.get("analysis") or {}).get("p_now")
        pct_s = f' = {"/".join(f"{pn[k]:.0f}" for k in SIGNS)} %' if pn else ""
        print(f'    Odds {fmt_triple(ev["odds"])}{pct_s}' + (f' (vid öppning {fmt_triple(start)})' if moved else "")
              + f'  |  Svenska folket {fmt_triple(ev["svenska_folket"], "%")}')
        st = ev.get("stats")
        if not st:
            continue
        if "error" in st:
            print(f'    ! {st["error"]}')
            continue
        if st.get("status"):
            print(f'    ! Status hos ESPN: {st["status"]}')
        for side in ("home", "away"):
            t = st[side]
            parts = []
            if t["rank"] is not None:
                q = f' (fjärdedel {t["quartile"]})' if t.get("quartile") else ""
                where = "gruppen" if (t.get("group_size") or 99) < 10 else "tabellen"
                parts.append(f'{num(t["rank"])}:a i {where}{q}')
            rec = t.get("record")
            if rec and rec["played"]:
                parts.append(f'{rec["points"]}p på {rec["played"]} m ({rec["w"]}V {rec["d"]}O {rec["l"]}F)')
                gd = to_float(t["goal_diff"])
                if gd is not None:
                    parts.append(f'målskillnad {"+" if gd > 0 else ""}{num(gd)}')
            elif rec is not None:
                parts.append("inga matcher spelade i turneringen än")
            print(f'    {t["name"]}: {", ".join(parts)}')
            if not (t.get("form_home") or {}).get("games") and not (t.get("form_away") or {}).get("games"):
                # Inga ligamatcher än (t.ex. Nations League före första omgången):
                # visa senaste matcherna i alla tävlingar, utan viktning.
                print("      Inga ligamatcher än - senaste matcher i alla tävlingar (ej viktade):")
                for g in sorted(t.get("last5", []), key=lambda g: g["date"], reverse=True):
                    where = "hemma mot" if g["home"] else "borta mot"
                    comp = f'  [{g["competition"]}]' if g["competition"] else ""
                    print(f'        {g["date"]} {where} {g["opponent"]} {g["score"]} ({g["result"]}){comp}')
                continue
            # Hemmalaget spelar hemma och bortalaget borta - den formen markeras.
            for key, label, current in (("form_home", "Hemma", side == "home"),
                                        ("form_away", "Borta", side == "away")):
                print("      " + format_form(t.get(key), label, current))
        print("    " + format_rest(st["home"], st["away"]))
        xg = ev.get("xg") or {}
        if xg.get("model"):
            for side, venue, label in (("home", "home", "hemma"), ("away", "away", "borta")):
                b = xg.get(side) or {}
                s, v = b.get("all"), b.get(venue)
                name = (st.get(side) or {}).get("name") or ev.get(side)
                if s:
                    tb = xg.get(f"table_{side}") or {}
                    vs = f' | {label} {v["xg"]:.1f}-{v["xga"]:.1f} ({v["n"]} m)' if v else ""
                    tab = f' | tabell {tb["rank_all"]}, xG-tabell {tb["xrank_all"]}' if tb else ""
                    print(f'    xG {name}: {s["xg"]:.1f}-{s["xga"]:.1f}/m ({s["n"]} m){vs} | xPts {s["xpts"]:.1f}/m, '
                          f'poäng {s["pts"]:.1f}/m | xV-xO-xF {s["xw"]:.1f}-{s["xd"]:.1f}-{s["xl"]:.1f} '
                          f'mot {s["w"]}-{s["d"]}-{s["l"]}{tab}')
                    extra = []
                    if s["shots"] is not None:
                        extra.append(f'skott {s["shots"]:.1f}-{s["shots_a"]:.1f}')
                    if s["sot"] is not None:
                        extra.append(f'på mål {s["sot"]:.1f}-{s["sot_a"]:.1f}')
                    if s["xg_per_shot"] is not None:
                        extra.append(f'xG/skott {s["xg_per_shot"]:.2f}')
                    extra.append(f'avslut {s["finishing"]:+.1f} mål, försvar {s["keeping"]:+.1f} mål mot xG')
                    extra.append(f'nollor {s["cs"]}/{s["xcs"]:.1f}, båda gör mål {100 * s["btts"]:.0f}/{100 * s["xbtts"]:.0f} %, '
                                 f'Ö2,5 {100 * s["o25"]:.0f}/{100 * s["xo25"]:.0f} %')
                    print("       " + " | ".join(extra))
            m, lam = xg["model"], xg.get("lambda") or (0, 0)
            me = xg.get("model_extra") or {}
            tops = ", ".join(f"{sc} {p:.0f}%" for sc, p in me.get("top_scores", []))
            mp, xpm = xg.get("model_xpts"), xg.get("xpts_match") or (0, 0)
            if mp:
                print(f'    xPts-prognos: {mp["one"]:.0f}/{mp["x"]:.0f}/{mp["two"]:.0f} % | '
                      f'förväntade xPts i matchen {xpm[0]:.1f}-{xpm[1]:.1f}')
            print(f'    xG-prognos: {m["one"]:.0f}/{m["x"]:.0f}/{m["two"]:.0f} % | över 2,5 mål {me.get("over25", 0):.0f} % | '
                  f'båda gör mål {me.get("btts", 0):.0f} % | förväntade mål {lam[0]:.1f}-{lam[1]:.1f} | troligast {tops}')
        elif xg.get("error"):
            print(f'    {xg["error"]}')
        for side in ("home", "away"):
            t = st.get(side) or {}
            ab = t.get("absent") or []
            if ab:
                txt = ", ".join(f'{p["name"]} ({absent_why(p) or p.get("status") or "?"}, '
                                f'{p.get("starts", 0)}/{p.get("games", 0)} starter)' for p in ab[:6])
                print(f'    Saknas {t.get("name")}: {txt}{" ..." if len(ab) > 6 else ""}')
        if st["h2h"]:
            print("    Inbördes:")
            for g in st["h2h"]:
                print(f"      {g}")


def format_rest(home, away):
    parts, days = [], []
    for t in (home, away):
        r = t.get("rest")
        if not r:
            parts.append(f'{t["name"]} okänt')
            days.append(None)
            continue
        days.append(r["days"])
        if r["days"] > 30:
            parts.append(f'{t["name"]} {r["days"]} dagar (senast {r["date"]})')
        else:
            comp = "" if r["competition"] in ("liga", "") else f'{r["competition"]}, '
            parts.append(f'{t["name"]} {r["days"]} dagar ({comp}{r["opponent"]} {r["date"][5:]})')
    line = "Vila: " + " | ".join(parts)
    if None not in days and abs(days[0] - days[1]) >= REST_FLAG_DAYS and min(days) <= 7:
        rested = home["name"] if days[0] > days[1] else away["name"]
        line += f"   <- {rested} har {abs(days[0] - days[1])} dagar mer vila"
    return line


def format_form(f, label, current):
    mark = " <-" if current else "   "
    head = f"{label}{mark}"
    if not f or not f["games"]:
        return f"{head}  inga spelade ligamatcher än"
    if f["weighted"] is not None:
        score = f'formbetyg {grade_s(f["per_game"]):>4} ({grade_word(f["per_game"])})'
    else:
        score = "  -  (tabell saknas, kan inte vikta)"
    games = " | ".join(
        f'{g["opp_name"]}'
        + (f'({num(g["opp_rank"])})' if g["opp_rank"] is not None else "")
        + f' {g["gf"]}-{g["ga"]}'
        + (f' {"+" if g["points"] > 0 else ""}{g["points"]}' if g["points"] is not None else "")
        for g in f["games"])
    return f'{head}  {f["form"]:<{FORM_GAMES}}  {score}   {games}'


# --------------------------------------------------------------------------- #
# Val av datum och omgång
# --------------------------------------------------------------------------- #
def reuse_saved_odds(draw, out):
    """
    Svenska Spel tar bort aktuellt odds strax före avspark. Har omgången körts tidigare finns oddset kvar
    i den sparade json-filen - använd det (senast kända oddset) i stället för startoddset.
    """
    missing = [ev for ev in draw["events"] if ev.get("odds_from_start")]
    if not missing:
        return
    path = Path(out) / f'{draw["product"]}_{close_date(draw)}_{draw["draw_number"]}.json'
    try:
        saved = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return
    when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M")
    by_nr = {e.get("nr"): e for e in saved.get("events") or []}
    for ev in missing:
        old = by_nr.get(ev["nr"]) or {}
        if (old.get("home"), old.get("away")) != (ev["home"], ev["away"]) or old.get("odds_from_start"):
            continue
        if any(v for v in (old.get("odds") or {}).values()):
            ev["odds"] = dict(old["odds"])
            ev.pop("odds_from_start", None)
            ev["odds_from_saved"] = old.get("odds_from_saved") or when


def close_date(draw):
    return (draw.get("close_time") or "")[:10]


def parse_date_input(s, today=None):
    """'' -> idag. Tar emot 2026-09-27, 27/9, 27/9/2026, 27.9, imorgon, idag. None om ogiltigt."""
    from datetime import date, timedelta
    today = today or date.today()
    s = (s or "").strip().lower()
    if s in ("", "idag", "i dag"):
        return today.isoformat()
    if s in ("imorgon", "i morgon"):
        return (today + timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = (2000 + int(y) if len(y) == 2 else int(y)) if y else today.year
        try:
            return date(year, mo, d).isoformat()
        except ValueError:
            return None
    return None


def describe_draw(draw):
    """En rad som gör det lätt att skilja omgångarna åt."""
    close = draw.get("close_time") or ""
    try:
        close = datetime.fromisoformat(close.replace("Z", "+00:00")).strftime("%a %d/%m %H:%M")
        close = close.replace("Mon", "mån").replace("Tue", "tis").replace("Wed", "ons").replace("Thu", "tor") \
                     .replace("Fri", "fre").replace("Sat", "lör").replace("Sun", "sön")
    except ValueError:
        pass
    leagues = []
    for ev in draw["events"]:
        if ev.get("league") and ev["league"] not in leagues:
            leagues.append(ev["league"])
    first = draw["events"][0] if draw["events"] else None
    first_s = f'{first["home"]} - {first["away"]}' if first else ""
    lg = ", ".join(leagues[:3]) + (f" +{len(leagues) - 3}" if len(leagues) > 3 else "")
    comment = draw.get("comment") or ""
    return (f'{draw.get("product_name") or draw["product"]} {draw["draw_number"]}  |  stänger {close}  |  '
            f'{len(draw["events"])} matcher: {lg}  |  1:a match {first_s}'
            + (f'  |  {comment}' if comment and comment.lower() not in (draw.get("product_name") or "").lower() else ""))


def choose_system(arg=None):
    """Sätter SYSTEM från --system eller frågar. Enter = automatiskt efter radbudget."""
    SYSTEM.clear()
    text = arg
    while True:
        if text is None:
            if not sys.stdin.isatty():
                return
            forval = ", ".join(f"{k.capitalize()} {v['hel']}-{v['halv']}" for k, v in DEFAULT_SYSTEMS.items() if v)
            print(f"\nSystem? Enter = förval ({forval}). Exempel:")
            print("  4-3          4 helgarderingar + 3 halvgarderingar (432 rader)")
            print("  4h 3b        samma sak")
            print("  4-3 r12      samma system reducerat, garanti 12 rätt om rätt rad finns i systemet")
            print("  5-4 max 200  5 hel + 4 halv, de 200 troligaste raderna")
            print("  150          automatiskt men med 150 rader")
            text = input("> ")
        try:
            sysval = parse_system(text)
        except ValueError as e:
            print(f"  {e}")
            if arg is not None:
                return
            text = None
            continue
        if "rader" in sysval:
            globals()["RADER"] = max(1, sysval.pop("rader"))
        SYSTEM.update(sysval)
        if SYSTEM:
            print(f"  System: {system_label()}")
        return


def choose_draws(draws, date_arg=None, take_all=False):
    interactive = sys.stdin.isatty()
    by_date = {}
    for d in draws:
        by_date.setdefault(close_date(d), []).append(d)

    wanted = parse_date_input(date_arg) if date_arg else None
    if date_arg and not wanted:
        print(f"Förstod inte datumet '{date_arg}'.")
    while True:
        if wanted is None:
            if interactive:
                wanted = parse_date_input(input("\nDatum för spelstopp (Enter = idag, t.ex. 2026-09-27, 27/9, imorgon): "))
                if wanted is None:
                    print("Förstod inte datumet - försök igen.")
                    continue
            else:
                wanted = parse_date_input("")
        if wanted in by_date:
            break
        print(f"\nInga öppna omgångar med spelstopp {wanted}. Öppna omgångar finns dessa datum:")
        for d in sorted(by_date):
            names = {}
            for x in by_date[d]:
                n = x.get("product_name") or x["product"]
                names[n] = names.get(n, 0) + 1
            print(f"  {d}: " + ", ".join(f"{n}" + (f" ({c} st)" if c > 1 else "") for n, c in names.items()))
        if not interactive:
            return []
        wanted = None

    day = sorted(by_date[wanted], key=lambda d: (d.get("close_time") or "", d.get("product_name") or ""))
    if len(day) == 1 or take_all or not interactive:
        return day

    print(f"\nÖppna omgångar med spelstopp {wanted}:")
    for i, d in enumerate(day, 1):
        print(f"  {i:>2}) {describe_draw(d)}")
    while True:
        s = input("Välj omgångar (t.ex. 1 eller 1,3 - Enter = alla): ").strip()
        if not s:
            return day
        try:
            idx = sorted({int(x) for x in re.split(r"[,\s]+", s) if x})
            if all(1 <= i <= len(day) for i in idx):
                return [day[i - 1] for i in idx]
        except ValueError:
            pass
        print(f"Skriv nummer mellan 1 och {len(day)}, t.ex. 1,3.")


# --------------------------------------------------------------------------- #
def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="Hämta poolspel från Svenska Spel + lagstatistik från ESPN")
    ap.add_argument("products", nargs="*", help=f"Ett eller flera av: {', '.join(PRODUCTS)} - räcker med s, e, t (standard: alla)")
    ap.add_argument("--no-stats", action="store_true", help="Hämta bara kupongerna")
    ap.add_argument("--utan-skador", action="store_true", help="Hoppa över skador/avstängningar (går fortare)")
    ap.add_argument("--dump-raw", action="store_true", help="Spara rå-JSON i mappen raw/")
    ap.add_argument("--out", default="kuponger", help="Mapp för JSON-utdata och webbsidor")
    ap.add_argument("--no-open", action="store_true", help="Öppna inte webbsidan automatiskt")
    ap.add_argument("--datum", help="Spelstoppsdatum, t.ex. 2026-09-27, 27/9 eller imorgon (annars frågar scriptet)")
    ap.add_argument("--alla", action="store_true", help="Ta alla omgångar det datumet utan att fråga")
    ap.add_argument("--allt", action="store_true", help="Ta alla öppna omgångar, alla datum (för webbsidan på GitHub)")
    ap.add_argument("--nasta", action="store_true", help="Ta närmaste öppna omgång utan att fråga om datum (bra i mobilen)")
    ap.add_argument("--rader", type=int, default=RADER, help=f"Radbudget för kupongförslaget (standard {RADER})")
    ap.add_argument("--system", help="System, t.ex. '4-3' (4 hel 3 halv), '4-3 r12' (reducerat, garanti 12), "
                                     "'5-4 max 200'. Utan det här frågar scriptet.")
    ap.add_argument("--kryss", type=float, default=KRYSS_VIKT,
                    help="Extra vikt på X i procent när kupongen byggs, t.ex. 20 (standard 0)")
    args = ap.parse_args()
    globals()["RADER"] = max(1, args.rader)
    globals()["KRYSS_VIKT"] = max(0.0, args.kryss)
    def product_name(p):
        """Kortnamn: s = stryktipset, e = europatipset, t = topptipset (även 'stryk', 'euro', 'topp')."""
        p = p.lower()
        for full in PRODUCTS:
            if full.startswith(p):
                return full
        return p
    args.products = list(dict.fromkeys(product_name(p) for p in args.products)) or PRODUCTS
    invalid = [p for p in args.products if p not in PRODUCTS]
    if invalid:
        ap.error(f"okänt spel: {', '.join(invalid)} (välj bland {', '.join(PRODUCTS)})")

    espn = None if args.no_stats else Espn(args.dump_raw, args.utan_skador)
    fdnew = FDNew()
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    # 1) Hämta alla öppna omgångar
    all_draws = []
    for product in args.products:
        try:
            draws = fetch_draws(product, args.dump_raw)
        except requests.RequestException as e:
            print(f"Kunde inte hämta {product}: {e}", file=sys.stderr)
            continue
        if not draws:
            print(f"Inga öppna omgångar för {product} just nu.")
        all_draws += draws
    if not all_draws:
        return

    # 2) Välj datum och omgångar
    if args.nasta and all_draws and not args.datum:
        args.datum = min(close_date(d) for d in all_draws)
    selected = all_draws if args.allt else choose_draws(all_draws, args.datum, args.alla)
    choose_system(args.system)

    # 3) Hämta statistik och skriv ut
    xgdata = None if args.no_stats else XGData()
    for draw in selected:
        reuse_saved_odds(draw, out)
        if espn:
            for ev in draw["events"]:
                try:
                    ev["stats"] = espn.enrich(ev)
                except (KeyError, TypeError, AttributeError, ValueError) as e:
                    ev["stats"] = {"error": f"{type(e).__name__}: {e}"}
                # ESPN saknar matchen/ligan -> försök med football-datas resultat (tabell, form, vila)
                if (not ev["stats"] or ev["stats"].get("error")) and ev.get("start"):
                    sl = league_slug(ev.get("league"), ev.get("country"), ev.get("country_iso"))
                    if sl in FD_NEW:
                        try:
                            alt = fdnew.enrich(ev, sl)
                        except (KeyError, TypeError, ValueError) as e:
                            alt = {"error": f"football-data: {type(e).__name__}: {e}"}
                        if alt and not alt.get("error"):
                            ev["stats"] = alt
                elif ev["stats"] and not ev["stats"].get("h2h") and ev.get("start"):
                    # ESPN har matchen men inga inbördes möten (t.ex. Allsvenskan) -> ta dem från football-data
                    sl = ev["stats"].get("espn_league")
                    if sl in FD_NEW:
                        try:
                            ev["stats"]["h2h"] = fdnew.h2h(ev, sl)
                        except (KeyError, TypeError, ValueError):
                            pass
                try:
                    ev["xg"] = xgdata.enrich(ev)
                except (KeyError, TypeError, AttributeError, ValueError, ZeroDivisionError) as e:
                    ev["xg"] = {"error": f"xG: {type(e).__name__}: {e}"}
        # Förvalt system per spel (DEFAULT_SYSTEMS) om inget eget system angetts
        user_system = dict(SYSTEM)
        if not user_system:
            SYSTEM.update(default_system(draw))
            if draw["product"] in AUTO_SYSTEM_PRODUCTS:
                SYSTEM["auto"] = True   # webbsidan väljer systemförslaget (★) inom samma kostnad
        print_draw(draw)
        base = f'{draw["product"]}_{close_date(draw)}_{draw["draw_number"]}'
        path = out / f"{base}.json"
        path.write_text(json.dumps(draw, ensure_ascii=False, indent=2), "utf-8")
        html_path = write_html(draw, out / f"{base}.html")
        SYSTEM.clear()
        SYSTEM.update(user_system)
        print(f"\n    Sparat: {path}")
        print(f"    Webbsida: {html_path}")
        played = draw.get("_played")
        if played and played[1] is not None:
            rows_path = write_rows_file(draw, played[1], out / f"{base}_rader.txt")
            print(f"    Rader: {rows_path}  ({played[2]} rader)")
        if not args.no_open:
            if "com.termux" in os.environ.get("PREFIX", ""):
                # Android (Termux): öppna webbsidan i telefonens webbläsare
                import subprocess
                subprocess.run(["termux-open", str(Path(html_path).resolve())], check=False)
            else:
                import webbrowser
                webbrowser.open(Path(html_path).resolve().as_uri())


if __name__ == "__main__":
    main()

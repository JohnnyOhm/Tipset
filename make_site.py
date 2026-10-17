#!/usr/bin/env python3
"""
make_site.py - bygger webbplatsen (startsida + alla omgångar) av det tipset.py har sparat.

Kör:  python make_site.py kuponger site
Startsidan listar alla öppna omgångar (Stryktipset, Europatipset, Topptipset ...) med länk till varje sida.
"""
import html
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "kuponger")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "site")
HERE = Path(__file__).parent
SE = timezone(timedelta(hours=2))  # bara för visning av "uppdaterad"

BACK = ('<p style="margin:0 0 12px"><a href="./" style="text-decoration:none;font-weight:600">'
        '← Alla omgångar</a></p>')

WEEKDAYS = ["mån", "tis", "ons", "tors", "fre", "lör", "sön"]


def parse_time(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    draws = []
    for js in sorted(SRC.glob("*.json")):
        page = js.with_suffix(".html")
        if not page.exists():
            continue
        try:
            d = json.loads(js.read_text("utf-8"))
        except ValueError:
            continue
        close = parse_time(d.get("close_time"))
        if not close or close < now - timedelta(hours=3):   # stängda omgångar visas bara en liten stund till
            continue
        key = (d.get("product"), d.get("draw_number"))
        fund = d.get("fund") or {}
        draws.append({"key": key, "mtime": js.stat().st_mtime, "page": page, "close": close,
                      "name": d.get("product_name") or (d.get("product") or "").capitalize(),
                      "nr": d.get("draw_number"), "n": len(d.get("events") or []),
                      "extra": (fund.get("extra") or 0) + (fund.get("rollover") or 0),
                      "first": " · ".join(f'{e.get("home")}–{e.get("away")}' for e in (d.get("events") or [])[:2])})

    # samma omgång kan finnas flera gånger (spelstoppet flyttat e.d.) - ta den senast sparade
    latest = {}
    for x in draws:
        if x["key"] not in latest or x["mtime"] > latest[x["key"]]["mtime"]:
            latest[x["key"]] = x
    draws = sorted(latest.values(), key=lambda x: x["close"])
    cards = []
    for x in draws:
        target = OUT / x["page"].name
        text = x["page"].read_text("utf-8").replace("<main>", "<main>" + BACK, 1)
        target.write_text(text, "utf-8")
        local = x["close"].astimezone(SE)
        closed = x["close"] < now
        when = f'{WEEKDAYS[local.weekday()]} {local.day}/{local.month} kl {local:%H:%M}'
        extra = (f'<span class="x">Extrapengar {round(x["extra"]):,} kr</span>'.replace(",", " ")
                 if x["extra"] > 0 else "")
        cards.append(f'''<a class="c{' closed' if closed else ''}" href="{html.escape(target.name)}">
<div class="t">{html.escape(x["name"])} <span class="nr">{x["nr"]}</span></div>
<div class="w">{"Stängd" if closed else "Spelstopp"} {when} · {x["n"]} matcher {extra}</div>
<div class="m">{html.escape(x["first"])} …</div></a>''')

    updated = datetime.now(SE).strftime("%Y-%m-%d %H:%M")
    body = "\n".join(cards) or '<p class="empty">Inga öppna omgångar just nu.</p>'
    (OUT / "index.html").write_text(f"""<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tipset</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon-192.png"><link rel="apple-touch-icon" href="icon-192.png">
<meta name="theme-color" content="#1f7a4d">
<style>
:root{{--bg:#f7f7f5;--card:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#1f7a4d}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141414;--card:#1e1e1d;--text:#ececea;--muted:#a3a39e;--line:#33332f;--accent:#4fbf86}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:20px 14px 40px}}
h1{{margin:0 0 4px;font-size:26px}}.sub{{color:var(--muted);margin:0 0 18px;font-size:14px}}
a.c{{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px;
color:var(--text);text-decoration:none}}
a.c:active{{transform:scale(.99)}}a.c.closed{{opacity:.55}}
.t{{font-size:19px;font-weight:700}}.nr{{color:var(--muted);font-weight:400;font-size:15px}}
.w{{color:var(--accent);font-weight:600;font-size:14px;margin-top:2px}}.m{{color:var(--muted);font-size:14px;margin-top:4px}}
.x{{background:var(--accent);color:#fff;border-radius:6px;padding:0 6px;margin-left:4px;font-size:12px}}
.empty{{color:var(--muted)}}
</style></head><body><main>
<h1>Tipset</h1><p class="sub">Uppdaterad {updated}. Sidorna uppdateras automatiskt flera gånger om dagen.</p>
{body}
</main></body></html>""", "utf-8")

    (OUT / "manifest.webmanifest").write_text(json.dumps({
        "name": "Tipset", "short_name": "Tipset", "start_url": "./", "scope": "./", "display": "standalone",
        "background_color": "#f7f7f5", "theme_color": "#1f7a4d",
        "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}]}, ensure_ascii=False), "utf-8")
    for n in (192, 512):
        src = HERE / "ikoner" / f"icon-{n}.png"
        if src.exists():
            shutil.copy(src, OUT / src.name)
    print(f"Webbplats byggd: {len(draws)} omgångar -> {OUT}/index.html")


if __name__ == "__main__":
    main()

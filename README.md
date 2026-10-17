# Tipset

Hämtar Stryktipset, Europatipset och Topptipset från Svenska Spel och lagstatistik från ESPN,
och lägger ut en mobilvänlig sida med kupongbyggare. Uppdateras automatiskt flera gånger om dagen
(se `.github/workflows/tipset.yml`).

- `tipset.py` – själva scriptet (samma som körs på datorn)
- `make_site.py` – bygger startsidan med alla öppna omgångar
- `ikoner/` – appikonen för "Lägg till på startskärmen"

Uppdatera scriptet: ladda upp en ny `tipset.py` här (Add file → Upload files), så används den vid nästa körning.

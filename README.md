# vareska-data

Datový repozitář aplikace **Vareska** (Flutter, `C:\AI\Jídlo`). Každý den stáhne akční
nabídky českých řetězců (Albert, Lidl, Kaufland, Tesco, Billa, Penny, Globus), přiřadí je
k surovinám z katalogu aplikace a publikuje cenovou tabulku `prices.json`, kterou aplikace
stahuje přes GitHub Pages:

```
https://stanislavmudra-hue.github.io/vareska-data/prices.json
```

## Co a proč

Aplikace počítá cenu receptu a hledá akce. Bez aktuálních dat by používala jen odhady
(`refPriceCzkPerKg` v katalogu a kategoriální fallbacky). Tento repozitář drží:

| Cesta | Obsah |
|---|---|
| `docs/prices.json` | publikovaná tabulka (schéma v1, viz níže) – to, co aplikace stahuje |
| `docs/prices.schema.json` | JSON Schema tabulky |
| `docs/ratings.json` | denní export průměrných hodnocení receptů ze Supabase (viz níže) |
| `docs/auth/` | stránky účtu pro e‑mailové odkazy Supabase: `index.html` (Site URL), `reset.html` (nové heslo) |
| `docs/privacy.html` | zásady ochrany soukromí aplikace (kopie `docs/PRIVACY.md` z repozitáře aplikace; odkaz při registraci); generuje `tool/build_privacy.py` |
| `docs/admin/` | webový panel (stav zdrojů, historie běhů, fronta nepřiřazených položek) |
| `docs/data/` | data pro panel: `health.json`, `history.json`, `report.json`, `unmatched.json`, `matched.json`, `catalog/ingredients.json` |
| `docs/SOURCES.md`, `docs/kupi_terms.json` | rešerše zdrojů a hledané výrazy pro kupi.cz |
| `catalog/ingredients.json` | kopie katalogu surovin z aplikace (`assets/data/ingredients.json`) |
| `catalog/category_fallback.json` | kopie `categoryFallbackCzkPerKg` z aplikace |
| `pipeline/` | Python skripty (fetch → mapper → build → validate → report) |
| `out/` | mezivýsledky běhu (`offers.json`, `matched.json`, `build_report.json`, `report.json`, `history.json`, `last_seen.json`) |
| `.github/workflows/update.yml` | denní GitHub Actions workflow |
| `run_all.py` | spouštěč všech kroků (Makefile-style) |

## Jak to běží

```
fetch    pipeline/fetch*.py     stáhne nabídky (Globus/Lidl/Penny API, Albert/Billa Publitas text, kupi.cz)  → out/offers.json
mapper   pipeline/mapper.py     přiřadí nabídky k surovinám katalogu (normalizace jako v aplikaci)          → out/matched.json
build    pipeline/build_prices.py  sestaví docs/prices.json (v1)                                              → docs/prices.json, out/last_seen.json, out/build_report.json
validate pipeline/validate.py   JSON Schema + pravidla aplikace; při chybě končí s kódem 1
report   pipeline/report.py     out/report.json, out/history.json a kopie pro panel do docs/data/
```

Lokálně:

```bash
pip install -r requirements.txt
python run_all.py                       # celý běh
python run_all.py --skip fetch          # bez stahování (použije out/offers.json)
python run_all.py --only build,validate,report --today 2026-09-15
python run_all.py --test                # unit testy (tests/)
```

Pravidla slušného chování ke zdrojům: vlastní `User-Agent: Vareska-deals/1.0
(+https://github.com/stanislavmudra-hue/vareska-data)`, max. 1 požadavek/s na doménu,
respektování `robots.txt`, žádné přihlašování ani obcházení ochrany proti botům. Zdroj,
který blokuje (Tesco – Akamai, Kaufland – Cloudflare), se přeskočí a zapíše do reportu.

### Pravidla sestavení `prices.json` (`pipeline/build_prices.py`)

* `czkPerKg[surovina][obchod]` = **medián** aktuálních běžných (neakčních) cen; pokud existuje
  jen akce, použije se medián aktuálních akčních cen. „Aktuální“ = `validFrom ≤ dnes ≤ validTo`
  (chybějící mez je otevřená).
* `deals[]` = nabídky označené jako akce (výsledky z kupi.cz jsou akce vždy; u přímých zdrojů
  položky s přeškrtnutou/původní cenou nebo textem „akce“), pouze s explicitním `validTo ≥ dnes`;
  chybějící `validFrom` = dnes. Max. 3 nejlevnější akce na dvojici (surovina, obchod), celkem ≤ 5 000.
* **Carry-forward**: dvojice (surovina, obchod), která dnes nemá žádný řádek, si drží předchozí
  hodnotu z `docs/prices.json` nejvýše **21 dní** (datum posledního výskytu je v `out/last_seen.json`),
  pak se vypustí. Obchod, který dnes nemá vůbec žádné řádky (výpadek zdroje), si drží i dosud platné
  akce. Všechny přenesené hodnoty jsou v `out/report.json` → `build.stale`, vypuštěné v `build.dropped`.
* `categoryFallbackCzkPerKg` se kopíruje z `catalog/category_fallback.json`, `updated` = dnešní datum
  (Europe/Prague).
* Odmítnuté řádky (neznámá surovina/obchod, nekladná cena, cena mimo 0,1–10× `refPriceCzkPerKg`)
  se počítají v `build.skipped`.

### Vstup builderu – `out/matched.json` (výstup mapperu)

Seznam položek nebo objekt `{"items": [...], "review": [...], "unmatched": [...]}`. Položka:

```json
{"ingredientId": "maslo", "store": "lidl", "source": "lidl", "title": "Máslo Jihočeské 250 g",
 "czkPerKg": 159.6, "price": 39.9, "originalPrice": 49.9, "promo": true,
 "validFrom": "2026-09-14", "validTo": "2026-09-20", "status": "matched", "club": false}
```

Přijímají se i snake_case aliasy fetch vrstvy (`ingredient_id`, `price_per_kg`, `price_czk`,
`original_price_czk`, `is_promo`, `valid_from`, `valid_to`). Položky se `status` jiným než
`matched` builder ignoruje (report je počítá jako `review`/`unmatched`).

### Schéma `prices.json` (v1, `lib/models/price.dart`)

```json
{
 "v": 1,
 "updated": "2026-09-15",
 "czkPerKg": {"maslo": {"lidl": 159.6, "globus": 139.8}},
 "deals": [{"ingredientId": "maslo", "store": "globus", "czkPerKg": 139.8,
            "validFrom": "2026-09-09", "validTo": "2026-09-15", "title": "Máslo 250 g"}],
 "categoryFallbackCzkPerKg": {"dairy": 120, "...": 0}
}
```

Obchody: `albert | lidl | kaufland | tesco | billa | penny | globus`. Ceny jsou Kč/kg (u tekutin
Kč/l ≈ Kč/kg, kusové zboží mapper přepočítá přes `unitGrams` katalogu). Validátor kontroluje schéma,
výčet obchodů, kladné ceny, ISO data, existenci id surovin v katalogu, `validFrom ≤ validTo`,
neprošlé akce a limit 5 000 akcí.

## Hodnocení receptů – `docs/ratings.json`

Aplikace ukládá hodnocení (chuť, náročnost, „uvařím znovu“) do Supabase; veřejný pohled
`recipe_ratings_summary` drží jen průměry bez osobních údajů. Krok `ratings`
(`pipeline/export_ratings.py`, běží v workflow po `build`) ho jednou denně stáhne anon klíčem a zapíše:

```json
{"v": 1, "updated": "2026-09-16",
 "ratings": {"cz_hovezi_gulas": {"n": 23, "taste": 4.6, "difficulty": 2.3, "cookAgain": 0.87},
             "u:6f1c…":         {"n": 4,  "taste": 4.0, "difficulty": 3.5, "cookAgain": null}}}
```

* `n` = počet hodnocení, `taste`/`difficulty` = průměr 1–5 (jedno desetinné místo),
  `cookAgain` = podíl 0–1 odpovědí „ano“ (`null`, když nikdo neodpověděl); `u:<uuid>` jsou komunitní recepty.
* `--min-count N` (nebo `RATINGS_MIN_COUNT`) vynechá recepty s méně hlasy; výchozí 1 = vše (aplikace sama
  zobrazuje veřejné hvězdičky až od 3 hlasů).
* Krok **nikdy neshodí běh**: když tabulka ještě neexistuje (migrace neproběhla, HTTP 404), zapíše prázdnou
  tabulku; při výpadku sítě nebo chybě 5xx ponechá předchozí soubor a jen vypíše varování.
* Adresa a anon klíč Supabase jsou v `pipeline/export_ratings.py` (přepsatelné proměnnými `SUPABASE_URL`,
  `SUPABASE_ANON_KEY`); stejný klíč používá `docs/admin/config.js` a `docs/auth/config.js`. Je veřejný – práva
  určuje RLS v databázi.
* Aplikace čte `https://okolnik.cz/vareska-data/ratings.json` stejně jako `prices.json` (ETag, 24 h cache).

### Stránky účtu – `docs/auth/`

Supabase Auth posílá uživatele z e‑mailů na statické stránky tohoto webu (nastavení v dashboardu:
*Site URL* `https://okolnik.cz/vareska-data/auth/`, *Redirect URLs* `…/auth/reset.html` na obou hostech):

* `auth/index.html` – rozcestník „Vareska – účet“; odkaz na obnovení hesla přesměruje na `reset.html`
  (i když Supabase kvůli chybějící redirect URL pošle tokeny sem), potvrzení e‑mailu jen oznámí.
* `auth/reset.html` + `reset.js` – přečte parametry odkazu (`#access_token…&type=recovery`, `?token_hash=…`
  nebo `?code=…`), odstraní je z adresy, ověří je přes `supabase-js` a po zadání hesla 2× zavolá
  `auth.updateUser({password})` (případně `PUT /auth/v1/user`). Texty česky, funguje na GitHub Pages bez buildu.

## Rozvrh

Workflow `update-prices` běží denně v **02:00 UTC** (04:00 Praha v letním čase, 03:00 v zimním)
a ručně přes *Actions → update-prices → Run workflow* (volba `skip_fetch` znovu použije uložené
nabídky). Letáky řetězců se mění ve středu (Albert, Billa, Penny, Globus, Kaufland, Tesco) a
v pondělí + čtvrtek (Lidl); nové letáky jsou online obvykle den předem, takže ranní běh je stihne.

Mezi `build` a `validate` běží krok `ratings` (export hodnocení, viz výše).
Po úspěšném běhu workflow commituje `docs/**` a `out/**` (`chore: prices YYYY-MM-DD`), pokud se
něco změnilo. Když validace selže, nic se necommituje a běh je červený; report a panel se přesto
zapíší (krok *Report* běží vždy), takže chybu uvidíte i v `docs/data/health.json` po dalším
úspěšném běhu, případně v artefaktu `pipeline-out` daného běhu.

## Nasazení (GitHub Pages)

Používá se klasický režim „Pages z větve“:

1. *Settings → Pages → Build and deployment → Source: **Deploy from a branch***.
2. Branch: **master** (výchozí větev repozitáře), Folder: **/docs**. Uložit.
3. `docs/.nojekyll` je v repozitáři (Jekyll se přeskočí, publikují se soubory tak, jak jsou).

Každý push do `master` pak během ~1 minuty publikuje `docs/` na
`https://stanislavmudra-hue.github.io/vareska-data/`. Workflow nepotřebuje žádný deploy krok
(alternativa `actions/upload-pages-artifact` + `deploy-pages` není potřeba).

## Jak aplikace data používá

* Aplikace má v assetech vlastní `assets/data/prices.json`; při startu stáhne
  `https://stanislavmudra-hue.github.io/vareska-data/prices.json`, uloží ji do cache a použije
  ji, pokud má **novější `updated`** než bundlovaná tabulka (`StaticDealsProvider.replace`).
* `czkPerKg` dává běžné ceny po obchodech; `deals` je přepisují pro svůj obchod, dokud platí
  `validFrom ≤ dnes ≤ validTo` (nejlevnější aktivní akce vyhrává). Cena za surovinu = medián přes
  vybrané obchody → medián přes všechny → `refPriceCzkPerKg` → `categoryFallbackCzkPerKg`.
* Proto musí být `updated` vždy dnešní datum a soubor musí projít validátorem – neplatný soubor
  by aplikace odmítla celý.

## Katalog surovin a re-sync

Mapper i validátor používají kopii katalogu `catalog/ingredients.json` (587 surovin: `id`,
`nameCs`, `aliases`, `category`, `unitGrams`, `densityGPerMl`, `refPriceCzkPerKg`, …). Když se
katalog v aplikaci změní (nová surovina, alias, referenční cena):

```bash
python run_all.py --sync-catalog "C:/AI/Jídlo"
git add catalog && git commit -m "chore: sync ingredient catalog"
```

Skript zkopíruje `assets/data/ingredients.json` → `catalog/ingredients.json` a
`categoryFallbackCzkPerKg` z `assets/data/prices.json` → `catalog/category_fallback.json`.
Report pak při dalším běhu zkopíruje katalog i do `docs/data/catalog/ingredients.json` pro panel.

## Panel (`docs/admin/`)

Statická stránka bez backendu (`docs/admin/index.html`), čte:

| Soubor | Obsah |
|---|---|
| `docs/prices.json` | publikovaná tabulka – počty cen a akcí po obchodech |
| `docs/data/health.json` | poslední běh (`run`: datum, trvání, nabídky, akce, oceněné suroviny) a stav zdrojů (`sources`: ok/přeskočen, počet nabídek, přiřazeno, požadavky, chyba, robots) |
| `docs/data/history.json` | jeden záznam na den běhu (`date`, `offers`, `perSource`, `matched`, `review`, `unmatched`, `deals`, `priced`, `stale`, `dropped`, `ok`) – graf historie |
| `docs/data/unmatched.json` | fronta ke kontrole: `review` (kandidáti s nízkou jistotou) a `unmatched` (bez kandidáta), s obchodem, zdrojem, cenou a návrhy |
| `docs/data/matched.json` | ořezaný seznam přiřazených řádků (kontrola, co se k čemu přiřadilo) |
| `docs/data/report.json` | plný report běhu včetně seznamu přenesených (stale) hodnot |
| `docs/data/catalog/ingredients.json` | katalog pro našeptávač surovin |

Ruční přiřazení z panelu se ukládají do `data/mappings.json` (přes GitHub API s tokenem
uživatele); mapper je při dalším běhu použije přednostně. Panel funguje i offline nad lokální
kopií (`python -m http.server -d docs 8000` → `http://localhost:8000/admin/`).

## Vývoj

* Python 3.11, UTF-8 bez BOM, LF. Kód a komentáře anglicky, texty panelu česky.
* Testy: `python -m unittest discover -s tests -v` (nebo `python run_all.py --test`).
* `pipeline/build_prices.py --matched out/matched.json --out docs/prices.json --today 2026-09-15`
  pro ruční sestavení; `pipeline/validate.py docs/prices.json` pro kontrolu libovolného souboru.

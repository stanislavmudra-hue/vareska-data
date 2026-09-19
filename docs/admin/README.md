# Vareska – admin panel (docs/admin)

Statická stránka bez build kroku (vanilla JS), která zobrazuje výstupy pipeline
cen a akcí a umožňuje ručně přiřazovat nepřiřazené produkty k surovinám.

* GitHub Pages: `https://stanislavmudra-hue.github.io/vareska-data/admin/`
* Lokálně: `cd docs && python -m http.server 8000` → `http://localhost:8000/admin/`
  (stránka načítá data relativně: `../prices.json`, `../data/*.json`, proto musí být
  servírován celý adresář `docs/`, ne jen `docs/admin/`).

## Záložky

| Záložka | Co dělá | Data |
|---|---|---|
| **Přehled** | poslední běh (datum, trvání, počty), stav zdrojů, karta **Trhy** (řádek na trh: měna, datum tabulky, oceněné suroviny, akce, fronta, zdroje ok/chyba, řetězce bez zdroje, odkaz na `prices/<trh>.json`), tabulka nabídek per zdroj/obchod, graf historie (inline SVG), tlačítko **Spustit teď** (`workflow_dispatch`) | `../data/health.json`, `../data/markets.json`, `../data/history.json`, `../prices.json` |
| **Fronta** | nepřiřazené a „ke kontrole“ položky, návrhy top‑3, vyhledávání v katalogu (folding + stemming shodné s aplikací), **Přiřadit / Ignorovat** → fronta změn → **Uložit N změn** (jeden commit do `data/mappings.json`) | `../data/unmatched.json`, katalog surovin |
| **Ceny a akce** | prohlížeč `prices.json`: filtr podle suroviny, obchody, tabulka Kč/kg (nejnižší cena zeleně, aktivní akce označena ▲), seznam akcí s platností | `../prices.json` |
| **Kontrola receptů** | zobrazí `docs/data/qa/sources_report.md` a `content_stats.md`, pokud existují (jinak placeholder) | `../data/qa/*.md` |
| **Moderace** | přihlášení moderátora (e‑mail + heslo účtu z aplikace), fronta komunitních receptů (`user_recipes`, filtr podle stavu, náhled fotky, autor, suroviny a postup), tlačítka **Schválit / Zamítnout** (s poznámkou pro autora), přepínač **Ověřeno** u schválených receptů, stav účtu autora + **Zablokovat / Odblokovat autora**, sekce **Hodnocení** – nejlépe hodnocené recepty z `recipe_ratings_summary` | Supabase (`config.js`, `moderation.js`) |
| **Nahlášení** | nahlášení z aplikace seskupená podle cíle (recept / uživatel / vestavěný recept): důvody s počty, poslední poznámka, počet nahlašujících, náhled receptu; tlačítka **Zamítnout / Vyřešit / Skrýt recept / Zablokovat**; sekce **Uživatelé** – hledání účtů a omezení (ban) | Supabase, migrace 0002 (`moderation.js`) |
| **Nastavení** | GitHub token, owner/repo/větev/workflow, test připojení, diagnostika načtených souborů | `localStorage` |

Změny uložené z fronty se **projeví až v příštím běhu pipeline** – panel jen zapíše
pravidlo do `data/mappings.json`, samotné `prices.json` přegeneruje workflow.

## Nastavení GitHub tokenu (fine‑grained PAT)

Token se ukládá pouze v `localStorage` prohlížeče a posílá se výhradně na `https://api.github.com`.

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine‑grained tokens → Generate new token**.
2. *Token name*: např. `vareska-admin`; *Expiration*: podle uvážení (např. 90 dní).
3. *Repository access*: **Only select repositories** → `stanislavmudra-hue/vareska-data`.
4. *Permissions → Repository permissions*:
   * **Contents: Read and write** – zápis `data/mappings.json` přes Contents API,
   * **Actions: Read and write** – spuštění workflow (`workflow_dispatch`) a čtení stavu běhů,
   * (Metadata: Read se přidá automaticky).
5. **Generate token** a zkopírujte hodnotu (`github_pat_…`).
6. V panelu → **Nastavení**: vložte token, zkontrolujte owner (`stanislavmudra-hue`), repo (`vareska-data`),
   větev (výchozí `main`; test připojení ji doplní podle repozitáře), název workflow souboru
   (`update.yml` – soubor v `.github/workflows/`, musí mít `on: workflow_dispatch`) → **Uložit nastavení** → **Otestovat připojení**.
7. Tlačítko **Zapomenout token** token z prohlížeče odstraní.

Na sdíleném počítači token neukládejte; tento panel nemá žádný backend, kdo má token, může zapisovat do repozitáře.

## Moderace (Supabase)

Záložka **Moderace** je jediná část panelu, která mluví s backendem aplikace (Supabase, projekt v EU).
Používá knihovnu `supabase-js` z CDN (`https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2`) a veřejný
*anon* klíč z `docs/admin/config.js` (stejný soubor je i v `docs/auth/config.js`; oba musí zůstat shodné).
Klíč je veřejný – co smí kdo dělat, určuje Row Level Security v databázi (viz `docs/BACKEND.md` v repozitáři aplikace).

* **Přihlášení**: e‑mail + heslo účtu založeného v aplikaci Vareska. Práva moderátora přiděluje správce
  projektu spuštěním `supabase/seed_moderator.sql` (tabulka `moderators`); panel volá `rpc('is_moderator')`
  a bez práv frontu nezobrazí. Přihlášení se ukládá jen v tomto prohlížeči (`localStorage`, klíč `vareska.admin.auth`).
* **Fronta**: `user_recipes` podle stavu (`pending` výchozí; `approved`, `rejected`, `draft`, vše), autor přes
  `profiles_public` (embed `author:profiles_public(username, display_name)`, při `PGRST200` druhý dotaz),
  fotka z veřejného bucketu `recipe-photos` (`/storage/v1/object/public/recipe-photos/<author>/<id>.jpg`),
  názvy surovin z katalogu (`catalogById` z ostatních záložek), postup s minutami.
* **Schválit** → `update({status:'approved'})` (server doplní `published_at`); **Zamítnout** → `update({status:'rejected',
  moderation_note})` – poznámka je povinná (min. 3 znaky, max. 500), autor ji uvidí v aplikaci. Guard trigger
  na serveru hlídá, že moderátor mění jen `status`/`moderation_note`.
* **Hodnocení**: `recipe_ratings_summary` (čte i nepřihlášený) seřazené podle průměrné chuti a počtu hlasů,
  přepínač „jen s ≥ 3 hodnoceními“; komunitní recepty (`u:<uuid>`) se doplní názvem, pokud jsou schválené.
* **Ověřeno** (migrace 0002): u schváleného receptu přepínač 🛡 Ověřeno → `rpc('set_recipe_verified', {p_id, p_verified})`;
  aplikace pak u receptu ukazuje štít. Značka se na serveru zruší, když recept přestane být schválený.
* **Autor**: u každého receptu panel dohledá stav účtu autora (`rpc('search_profiles', {p_query: <id>})`, jen moderátor)
  a nabídne **Zablokovat autora** (`rpc('ban_user', {p_user, p_reason})`, důvod povinný – autor ho uvidí v aplikaci;
  server zároveň zamítne jeho čekající recepty a schválené skryje) nebo **Odblokovat** (`rpc('unban_user')`).
* **Suroviny**: řádky s `ingredientId` se překládají katalogem; vlastní suroviny bez id (pole `name`) se vypíší tak, jak je autor napsal.
* **Degradace**: bez knihovny (offline / blokovaný CDN) se zobrazí upozornění; když migrace v Supabase ještě
  neproběhla (HTTP 404 / `42P01`), panel napíše *Backend zatím není nasazen*; výpadek sítě → *Připojení není k dispozici*.
  Když chybí jen migrace **0002** (rpc `moderation_counts`, `reports_overview`… → `PGRST202` / `42883` / 404), panel napíše
  *Migrace 0002 není spuštěna*, skryje Ověřeno a zákazy a fronta receptů dál funguje jako dřív (počet čekajících se
  spočítá dotazem `head`).

## Nahlášení (Supabase, migrace 0002)

Záložka **Nahlášení** sdílí přihlášení se záložkou Moderace a používá rpc z `docs/BACKEND.md` §4.10 (všechny jen pro
účty v `moderators`):

* **Odznak v záložkách**: `rpc('moderation_counts')` → `{pending_recipes, open_reports}` plní počty u záložek *Moderace* a *Nahlášení*.
* **Seznam**: `rpc('reports_overview', {p_status})` (`open` výchozí; `resolved`, `dismissed`) – jeden řádek na nahlášený cíl:
  typ cíle, název receptu / `@uživatel`, stav receptu, počet nahlášení (= počet nahlašujících, každý účet má na cíl nejvýš
  jedno otevřené nahlášení), důvody s počty (`spam`, `offensive`, `wrong_content`, `copyright`, `dangerous`, `other`),
  poslední poznámka nahlašujícího, první a poslední nahlášení. **Zobrazit recept** načte kartu receptu (stejnou jako ve
  frontě, včetně Schválit / Zamítnout / Ověřeno) přímo pod nahlášením.
* **Zamítnout** → `rpc('resolve_report', {p_id, p_status:'dismissed', p_note, p_whole_target:true})` – nahlášení bylo neoprávněné, obsah zůstává;
  **Vyřešit** → totéž se stavem `resolved`; **Skrýt recept** → `update user_recipes {status:'rejected', moderation_note}`
  (poznámka povinná, autor ji uvidí) a pak `resolve_report(resolved)`; **Zablokovat autora / uživatele** → `rpc('ban_user')`
  (důvod povinný) a pak `resolve_report(resolved)`. Každá akce uzavře **všechna otevřená nahlášení daného cíle**
  (`p_whole_target`). V seznamech *Vyřešená* / *Zamítnutá* je tlačítko **Znovu otevřít**.
* **Uživatelé**: `rpc('search_profiles', {p_query})` – uživatelské jméno / zobrazované jméno (část), přesný e‑mail nebo id
  (e‑mail se nikdy nevrací); prázdný dotaz = 50 nejnovějších účtů. Tabulka ukazuje datum založení, počty receptů
  (schválené / čekající), stav (aktivní / omezen od…) a důvod; tlačítko **Zablokovat / Odblokovat**.
* Chybové kódy z rpc (`not_moderator`, `report_not_found`, `not_approved`, `user_not_found`, `cannot_ban_self`,
  `cannot_ban_moderator`, `status_not_allowed`…) panel překládá do češtiny (`RULE_TEXT` v `moderation.js`).

## Formáty dat, které panel čte

Panel je tolerantní k chybějícím souborům (zobrazí placeholder) i k mírně odlišným názvům polí.
Doporučené tvary, které pipeline zapisuje do `docs/`:

### `docs/prices.json`
Kontrakt aplikace (v1): `{"v":1,"updated":"YYYY-MM-DD","czkPerKg":{ingredientId:{store:price}},"deals":[{ingredientId,store,czkPerKg,validFrom,validTo,title}],"categoryFallbackCzkPerKg":{…}}`.
Volitelné pole `url` u akce panel zobrazí jako odkaz.

### `docs/data/markets.json`

Přehled trhů (`pipeline/report.py` → `markets_summary`), zapisuje ho každý krok `report`:

```json
{"v": 1, "generatedAt": "2026-09-19T19:09:44Z",
 "markets": {"cz": {"label": "Česko", "currency": "CZK", "lang": "cs", "stores": ["albert", "..."],
                    "prices": "prices/cz.json", "panelDir": "", "date": "2026-09-19", "ok": true, "status": "ok",
                    "pricesUpdated": "2026-09-19", "priced": 239, "deals": 733, "offers": 4866,
                    "review": 748, "unmatched": 1206, "sourcesOk": 7, "sourcesError": 0,
                    "notFetched": [], "warnings": 0},
             "sk": {"...": "...", "panelDir": "sk/", "notFetched": ["tesco", "kaufland", "billa", "coopJednota", "terno", "fresh"]}}}
```

Ostatní záložky panelu (fronta, ceny, akce) ukazují český trh; data ostatních trhů leží ve
stejném formátu v `docs/data/<trh>/` (`health.json`, `history.json`, `report.json`, `unmatched.json`,
`matched.json`) a v `docs/prices/<trh>.json` (schéma v2, `perKg` v měně trhu).

### `docs/data/health.json`
```json
{
  "run": {"startedAt": "2026-09-15T06:00:03Z", "finishedAt": "2026-09-15T06:04:41Z", "durationSec": 278,
          "offers": 1734, "deals": 312, "priced": 401, "unmatched": 57, "ok": true},
  "sources": {
    "globus":   {"ok": true,  "items": 874, "deals": 120, "matched": 610, "unmatched": 21, "requests": 9, "durationSec": 14},
    "albert":   {"ok": false, "items": 0, "error": "HTTP 503 letaky.albert.cz"},
    "kaufland": {"status": "skipped", "note": "Cloudflare challenge – kupi fallback"}
  }
}
```
`sources` může být i pole objektů s polem `id`/`name`. Stav se bere z `ok` (bool), případně `status` (`ok`/`error`/`skipped`).

### `docs/data/history.json`
```json
{"runs": [{"date": "2026-09-15", "durationSec": 278, "offers": 1734, "deals": 312, "priced": 401, "unmatched": 57, "ok": true}]}
```
(nebo přímo pole). Graf zobrazuje posledních 60 běhů; neúspěšné (`ok:false`) červeně.

### `docs/data/unmatched.json`
```json
{"generated": "2026-09-15", "items": [
  {"title": "Máslo České 250 g", "store": "globus", "source": "globus-api", "price": 44.9, "originalPrice": 54.9,
   "pack": "250 g", "czkPerKg": 179.6, "url": "https://…", "validTo": "2026-09-20", "promo": true,
   "status": "review", "ingredientId": "maslo", "confidence": 0.82, "reasons": ["name:maslo"],
   "suggestions": [{"ingredientId": "maslo", "score": 0.82, "nameCs": "máslo"}, {"ingredientId": "maslo_prepustene", "score": 0.31}]}
]}
```
* `status`: `unmatched` (žádný kandidát) nebo `review` (kandidát `ingredientId` s nízkou jistotou – panel ho předvyplní).
* `suggestions` (nebo `candidates`) mohou být i prostá pole id; když chybí, panel dopočítá návrhy z katalogu.
* Identifikátor položky je `key`, pokud ho pipeline dodá, jinak `fold(title)|store`.
* `pipeline/report.py` zapisuje tvar `{"date":…, "review":[…], "unmatched":[…], "reviewTotal":N, "unmatchedTotal":N}` – panel ho čte také.

### Katalog surovin
Hledá se postupně `../data/catalog/ingredients.json`, `../catalog/ingredients.json`, `../data/ingredients.json`.
Tvar shodný s aplikací (`{"v":1,"ingredients":[{id,nameCs,namePluralCs,nameEn,aliases,category,refPriceCzkPerKg,…}]}` nebo prosté pole).

### `data/mappings.json` (zapisuje panel, čte `pipeline/mapper.py`)
```json
{"v": 1, "updated": "2026-09-15", "rules": [
  {"pattern": "kureci prsni rizky 1 kg", "store": "lidl", "ingredientId": "kureci_prsa", "note": "panel 2026-09-15", "title": "Kuřecí prsní řízky 1 kg"},
  {"pattern": "pilsner urquell 0 5 l plech", "store": "penny", "ingredientId": "ignore", "note": "panel 2026-09-15", "title": "Pilsner Urquell 0,5 l plech"}
]}
```
* Kontrakt mapperu (`load_rules`): `pattern` (text → folding, hledá se jako celé slovo/fráze ve foldovaném názvu nabídky;
  `re:…` nebo `/…/` = regulární výraz), `store` = obchod nebo `*` (všude), `ingredientId` = id z katalogu nebo `ignore`, `note` volný text.
  Pole `title` a `updated` jsou jen informativní.
* Panel používá jako `pattern` celý foldovaný název produktu (`fold` z `lib/logic/text_normalizer.dart`), tj. pravidlo platí přesně
  pro tento produkt (i při dalším výskytu). Obecnější pravidla (`"pattern": "maslo", "store": "*"`) lze dopsat ručně.
* Pravidlo se stejným `pattern` + `store` se přepisuje; ostatní obsah souboru zůstává zachován.
* Commit message: `panel: mapping <title>` (při více změnách `panel: mapping A, B, C +N (N pravidel)`).
* Při konfliktu SHA (někdo mezitím soubor změnil) panel soubor znovu načte a zápis zopakuje (max. 3×).
* Vyřízené položky panel lokálně skryje (localStorage, 7 dní) do doby, než je příští běh pipeline odstraní z `unmatched.json`.

### `docs/data/qa/sources_report.md`, `docs/data/qa/content_stats.md`
Volitelné Markdown zprávy z CI aplikace (nadpisy, seznamy, tabulky, kód). Bez nich záložka ukáže placeholder.

## Testy
`python -m pytest tests/test_admin_panel.py` (nebo `python tests/test_admin_panel.py`) – kontrola syntaxe JS (`node --check`),
párování HTML značek, existence odkazovaných souborů a shoda JS normalizéru s pravidly aplikace (přes `node`).
`python -m pytest tests/test_moderation_auth.py` – totéž pro záložky Moderace a Nahlášení (`moderation.js`, `config.js`) a stránky
`docs/auth/` (klasifikace chyb, formát surovin, řazení hodnocení, parsování odkazu pro obnovení hesla).
`node tests/moderation_helpers.test.js` – čisté pomocné funkce z `moderation.js` (`window.VareskaModeration`: klasifikace chyb
včetně kódů z 0002, vlastní suroviny, souhrn důvodů nahlášení, normalizace řádků `reports_overview` a `moderation_counts`).

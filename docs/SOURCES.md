# Deal sources – research report (R1)

Date of research: **2026-09-15 (Tuesday)**, probes run from a Czech residential IP with
`User-Agent: Vareska-deals/1.0 (+https://github.com/stanislavmudra-hue/vareska-data)`,
plain `urllib` (no cookies, no JS), ≤ 1 request/s per host. Every endpoint below was
fetched and its response inspected; nothing here is assumed from memory.

Target contract: `assets/data/prices.json` v1 (`czkPerKg{ingredientId{store}}`,
`deals[{ingredientId, store, czkPerKg, validFrom, validTo, title}]`),
stores `albert | lidl | kaufland | tesco | billa | penny | globus`.

---

## 0. Decision table

| Source | Decision | What we get | Structure | Requests / run | Bot protection (CZ IP) | US-IP risk | Risk |
|---|---|---|---|---|---|---|---|
| **Globus** – `www.globus.cz/api/v1/gsoa/actionOffers/houses/{house}/actionProductsCatalog` | **USE (primary)** | 874 promo items, price, old price, %, unit price per kg/l, validity | JSON, fully structured | 9 | Cloudflare (no challenge for our UA) | medium (CF may challenge DC IPs) | low |
| **Lidl** – `www.lidl.cz/q/api/category/…` (food category grid) | **USE (primary)** | ~500 in-store food products incl. Monday/Thursday offers; price, old price, "250 g, 100 g = 15,96 Kč", validFrom/validUntil (unix) | JSON, structured (unit text needs regex) | ~15 | Myra Security CDN (no block) | low–medium (unknown geo policy) | low |
| **Penny** – `www.penny.cz/api/product-discovery/categories/vsechny-akce-99000000/products` | **USE (primary, small)** | 60 web promo items (this + next week), price/loyalty price/old price, base unit price, validityStart/End | JSON, fully structured | 1–2 | none (istio/GCP) | low | low |
| **Albert** – Publitas `letaky.albert.cz/{slug}/spreads.json` | **FALLBACK (semi-structured text)** | full leaflet page text (product, pack, price digits, unit price), leaflet validity from albert.cz | text blocks per page; no hotspots | 2–4 | none (Apache / Publitas) | low | medium (text parsing) |
| **Billa** – Publitas `view.publitas.com/billa-cz/{slug}/spreads.json` | **FALLBACK (semi-structured text)** | same as Albert (Publitas), validity from billa.cz teaser | text per page | 2–4 | none | low | medium |
| **Kaufland** – `endpoints.leaflets.schwarz/v4/flyer?flyer_identifier=…&region_id=1000` | **SKIP for prices / USE for validity+PDF** | page images, PDF, loose keyword bag, offer dates | not structured | 1–3 | kaufland.cz itself: Cloudflare JS challenge even from CZ (403) → never fetch kaufland.cz | n/a (Schwarz endpoint on Myra) | low |
| **Tesco** – itesco.cz / nakup.itesco.cz | **SKIP** | – | – | – | Akamai: 403 for non-browser UA (robots.txt 200 only with a browser UA – we do not spoof) | high | – |
| **kupi.cz** – `/hledej?f={q}` and `/slevy/{slug}?f={q}` | **FALLBACK (all 7 chains, primary for Kaufland/Tesco/Albert/Billa)** | product, pack, price, price per unit, store, validity text, club flag, limits | HTML, stable class names | 150 (one query per ingredient term; all stores in one page) | Cloudflare (PRG POP, no challenge for our UA) | medium | medium (ToS grey; robots `User-agent: AI Disallow: /`) |
| **akcniceny.cz** – `/hledej?s={q}` → `/akce/{slug}/` | **SKIP (reserve)** | product-level price range + per-store detail pages | HTML | 2 per term | none today (200 with our UA; earlier report of blocking not reproduced) | low | medium |

**Recommended architecture**

```
primary  : globus (API) + lidl (API) + penny (API)           → ~26 requests, ~1.5 MB, fully structured
secondary: albert (Publitas text) + billa (Publitas text)     → ~8 requests, regex text parser, medium confidence
fallback : kupi.cz search, 150 ingredient terms, 1 rps        → ~3 min, ~75 MB, covers ALL chains incl. kaufland+tesco
metadata : kaufland Schwarz overview (validity dates), albert/billa/penny leaflet lists (validity dates)
```

Merge rule: per `(ingredientId, store)` prefer primary > secondary > kupi; keep the lowest czkPerKg
among rows valid today; write `deals[]` only for rows with an explicit `validTo`; the regular
(non-promo) price from Lidl's grid feeds `czkPerKg`.

Schedule: daily 06:00 UTC (leaflets change Wed morning for Albert/Billa/Penny/Globus/Kaufland/Tesco,
Mon + Thu for Lidl); next-week leaflets are already published on Mon/Tue, so a Tuesday run can
pre-load `validFrom` in the future.

---

## 1. Lidl — USE

### Official leaflet (images only)
* Landing: `https://www.lidl.cz/c/akcni-letak/s10008644` (`/letaky` is 404).
* Viewer = Schwarz "Flyer System" (`lidl.leaflets.schwarz`, API base `https://endpoints.leaflets.schwarz/v4`).
* Overview: `GET https://endpoints.leaflets.schwarz/v4/overview?client_locale=lidl/cs-CZ&region_id=0`
  → `categories[].subcategories[].flyers[]` with `name`, `title`, `startDate` (publication),
  `offerStartDate`, `offerEndDate`, `pdfUrl`, `flyerJson`.
  Today: "Akční leták OD PONDĚLÍ" 14.9–16.9, "Akční leták OD ČTVRTKA" 17.9–20.9, "Spotřební zboží" 14.9–20.9 (published 11.9).
* Flyer: `GET https://endpoints.leaflets.schwarz/v4/flyer?flyer_identifier=akcni-letak-od-ctvrtka-17-9-20-9-2026&region_id=0`
  → `flyer.pages[] {number, image, zoom, keyWords, altText, links[]}`; **`links` and `products` are empty for CZ** →
  no structured products. `keyWords` is an unordered word bag ("Máslo 7960 …") – do not parse.
* robots.txt (www.lidl.cz): disallows `/user-api/*`, `*search?q=*`, `*?offset=*`, `*id=*`, `*sort=*` … category pages allowed.

### Product grid API (structured, in-store food) – the source to use
The food category pages (`/h/…`) are server-rendered from an internal search API that is
reachable without auth:

```
GET https://www.lidl.cz/q/api/category/h/maso-a-drubez/h10095752?assortment=CZ&locale=cs_CZ&version=v2.0.0&pageId=10068374%2F10095752&fetchsize=200
Accept: application/json
```
Response `application/mindshift.search+json;version=2`:
```
{ numFound, offset, fetchsize, maxfetchsize:1000, items:[{ code, type:"product",
   gridbox:{ data:{ fullTitle, erpNumber, canonicalPath, category:"Food",
     keyfacts:{ description(html), wonCategoryPrimary:"…/Maso a drůbež/drůbež" },
     price:{ price:69.9, oldPrice:0|89.9, basePrice:{text:"400 g, 1 kg = 174,75 Kč"},
             packaging:{text:"1 l"}, discount:{discountText:"Super cena"|"-30%"|"Ušetřete* 31%"} },
     stockAvailability:{ badgeInfoV2:[{ badges:[{text, type}], validFrom:1789596000, validUntil:1789941599 }] },
     ribbons:[…] } } } ],
  facets:[{code:"category", topvalues:[…tree with counts…]}] }
```
* `badges[].type`: `IN_STORE` (standard assortment, no dates), `IN_STORE_PAST_DATE_RANGE` (offer running, start in past),
  `IN_STORE_FROM_FUTURE_DATE_RANGE` (upcoming), `IN_STORE_FROM_DATE_PAST` (permanent since date), `AVAILABLE_ONLINE`.
  Timestamps are epoch seconds in Europe/Prague (1789336800 = 2026-09-14 00:00, 1789595999 = 2026-09-16 23:59:59).
* Unit/pack: parse `basePrice.text` and `packaging.text` with regex
  `(\d+[,.]?\d*)\s*(g|kg|ml|l|ks)` for pack and `(1 kg|100 g|1 l|100 ml|1 kus) = (\d+[,.]\d+) Kč` for unit price;
  "cena za 100 g" means `price` is per 100 g (× 10 → per kg).
* Pagination: `fetchsize` is silently capped (498-item root category returned 108); use the **sub-categories**
  (all ≤ 104 items) instead of `offset` (robots disallows `?offset=`). Category ids (pageId root 10068374):
  Ovoce a zelenina `h10071012` (19), Maso a drůbež `h10095752` (63), Ryby `h10071050` (10),
  Sýry, mléčné výrobky a vejce `h10095761` (63), Pekárna `h10096086` (17), Spíž `h10096095` (37),
  Oleje, dochucovadla a omáčky `h10096110` (14), Hotová jídla `h10071020`, Cereálie `h10096153`,
  Mražené `h10071049` (77), Sladkosti `h10096205`, Nápoje `h10071022`, Káva/čaj `h10071683`.
  Slugs are cosmetic – the numeric id decides. Re-read the facet tree each run to detect id changes.
* Cadence: offers change **Monday** and **Thursday**; the API already carries next period's items with
  `FROM_FUTURE` dates, and the flyer overview publishes Friday for the following week.
* Risk: internal API (no ToS text found forbidding it; robots allows category paths). Myra CDN – geo policy
  for US runners unknown → keep a kupi fallback for Lidl.

---

## 2. Kaufland — SKIP prices (kupi fallback), USE Schwarz endpoint for validity/PDF

* `https://www.kaufland.cz/*` (even `/robots.txt`): **HTTP 403 Cloudflare "Vyžadováno ověření" JS challenge**
  for our UA from a Czech IP. Do not fetch kaufland.cz at all.
* Kaufland uses the same Schwarz flyer platform, regionalised:
  `GET https://endpoints.leaflets.schwarz/v4/overview?client_locale=kaufland/cs-CZ&region_id=1000`
  (region_id 0–100 return 0 flyers; 1000 works) → subcategories `KDZ`, `Hyper1`, `Wrapper1` with flyers
  "Kaufland 09.09.2026 - 15.09.2026", "16.09.2026 - 22.09.2026", monthly "02.09–29.09".
  `flyerJson` → `…/v4/flyer?flyer_identifier=kaufland-09-09-2026-15-09-2026-cccec0&region_id=1000`
  → 52 pages, images + `keyWords` bag, `links=[]`, `products=[]` → **not parseable for prices**.
* Cadence: **Wednesday → Tuesday**, next week's flyer already online on Tuesday.
* Structured Kaufland offers exist only behind the Cloudflare-protected site → **kupi.cz** (`/slevy/kaufland?f=…`, shop id 4).

---

## 3. Albert — FALLBACK (Publitas page text) + kupi

* `https://www.albert.cz/letak` → 301 → `https://www.albert.cz/aktualni-letaky` (Next.js). The page embeds
  `__NEXT_DATA__ → props.pageProps.apolloState` with `Leaflet:{id}` objects from GraphQL
  `getLeaflets({locationType:"HYPERMARKET"|"SUPERMARKET", onlyDefault:true})`:
  ```
  {"__typename":"Leaflet","id":"3358559","validityStartDateFormatted":"16.09.2026",
   "validityEndDateFormatted":"22.09.2026","validityEndDate":"22/09/2026 21:59:59",
   "title":"Albert - 38HM_acni_letak","locationType":"HYPERMARKET",
   "viewUrl":"https://letaky.albert.cz/38hm_akcni_letak/","downloadUrl":"https://view.publitas.com/90263/3358559/pdfs/….pdf",
   "documentType":"LEAFLET"|"CATALOG"}
  ```
  Slug pattern `{ISOweek}{hm|sm}_akcni_letak` (37hm = 9.9–15.9, 38hm = 16.9–22.9). Both HM and SM leaflets
  (hypermarket/supermarket) exist; SM prices can differ – use HM as canonical, SM as second row.
* Publitas viewer JSON (found in the reader JS, no auth):
  * `GET https://letaky.albert.cz/38hm_akcni_letak/spreads.json?page=1` → `[{pages:[{number, id, text, images{at1600…}, …}], originalPageNumbers}]`
    (paginated with `X-Next-Page` header; 31 spreads = all 60 pages came in one response).
  * `GET https://letaky.albert.cz/38hm_akcni_letak/page/{n}/hotspots_data.json` → `[]` on product pages
    (only `externalLink` hotspots on p.1) → **no product hotspots**.
  * `page.text` example: `"Kuře bez drobů\n• chlazené • 1 kg\n\n89,90/\n\n-44 %\n\n49\n\n90\n\n…Vejce z podestýlky M\n• 10 ks\n• 1 ks = 3,49 Kč bez Aplikace /\n2,99 Kč Aplikace\n\n59,90/\n\n-50 %\n\n29\n\n90"`.
    Parsing spec: split on blank lines; a product block = title line(s) followed by "• pack" bullets; old price
    is `NN,NN/`; new price is split into two tokens `49` + `90` (Kč + haléře) after the `-NN %` token;
    unit price bullet `• 1 kg = 199,80 Kč` / `100 g = …`. Confidence medium → tag rows `source:"albert-text"`.
* robots.txt (albert.cz): only `/nase-prodejny?q=*` and `/en/` disallowed. letaky.albert.cz has no robots.txt.
* Cadence: **Wednesday → Tuesday**; next week's Publitas document is published Monday ~07:00–11:00.
* Online shop: app-only ("Albert Online" via app stores); no web product listing.

---

## 4. Billa — FALLBACK (Publitas page text) + kupi

* `https://www.billa.cz/letaky` is 404; use `https://www.billa.cz/akcni-letaky` (Nuxt + Kontent.ai).
  Teasers carry the validity text: `Velký leták … Platí od středy 16. 9. do úterý 22. 9. 2026`,
  `Malý leták …`, plus catalogues (`/akcni-letaky/katalog-*`, `special-*`).
  Viewer pages: `/letaky-billa?tab=letaky-billa/velky-letak[-nasledujici]` embed the Publitas URL
  `https://view.publitas.com/billa-cz/velky-letak-9-9-15-9-2026/page/1` and the PDF download
  `https://view.publitas.com/64069/{pubId}/pdfs/{uuid}.pdf`.
* Publitas group `billa-cz` (id 64069): `https://view.publitas.com/billa-cz/` redirects to the newest
  publication; slug pattern `velky-letak-{d}-{m}-{d}-{m}-{yyyy}` / `maly-letak-…`.
  `spreads.json?page=1` (20 spreads / 39 pages) and `page/{n}/hotspots_data.json` (= `[]`) behave exactly
  as for Albert; `page.text` example: `"Jablko červené\n\nod 150 g\n\n100 g = od 39,95 Kč\n\n16,90 … Nektarinky\n\n79,90\n\nskládané, 1 kg\n\n-50%\n\n39,90\n79,90/"`.
  Billa text is noisier (letters of decorative words split: "NE\n\nJ V YŠ Š\nÍ") → parse only blocks that
  contain a `\d+,\d\d` price and a `(g|kg|ml|l|ks)` pack; tag `source:"billa-text"`. Club prices: "s Klubem / bez Klubu".
* robots.txt (billa.cz): only a Sitemap line (allow all). view.publitas.com: no robots restriction observed.
* Cadence: **Wednesday → Tuesday** (next leaflet online Monday).
* shop.billa.cz redirects to `/online-nakup` (delivery via Wolt/Foodora) – no product data.

---

## 5. Penny — USE (small) + kupi for the full leaflet

* `https://www.penny.cz/letaky` lists two FlippingBook leaflets hosted at
  `https://files.rewe.co.at/PennyIntLeaflet/CZ/{dd_mm_yyyy}_{zs|sf}/` (e.g. `09_09_2026_zs`, `16_09_2026_sf`).
  FlippingBook HTML contains only an SEO text dump (unordered prices then names) and page images; the text
  layer files (`files/assets/common/page-textlayers/…`) are not exposed (404) → not parseable.
* **Structured web promotions** (commercetools, same "ws-" platform as billa.cz):
  ```
  GET https://www.penny.cz/api/product-discovery/categories/vsechny-akce-99000000/products?page=0&pageSize=100
  ```
  → `{facets, count, offset, total:60, results:[…], isTotalTruncated}`; item:
  ```
  {"name":"Tvaroh tučný Karlova Koruna","amount":"250","volumeLabelShort":"g","packageLabel":"Kus",
   "weight":0.25,"sku":"88-205412","slug":"tvaroh-tucny-karlova-koruna-88205412",
   "category":"MLÉČNÉ VÝROBKY","parentCategories":[[…],[{"name":"VŠECHNY AKCE"},{"name":"Web od 09.09.2026"}]],
   "price":{"baseUnitShort":"g","basePriceFactor":"100",
            "regular":{"value":1290,"perStandardizedQuantity":516,"promotionType":"FROM"},
            "loyalty":{"value":1690,"perStandardizedQuantity":2113,"tags":["SO"]},   // optional PENNY karta price
            "crossed":2190,"discountPercentage":-41,"lowestPrice":1290,
            "validityStart":"2026-09-09","validityEnd":"2026-09-15"}}
  ```
  Prices are **integers in haléře** (1290 = 12,90 Kč); `perStandardizedQuantity` is per `basePriceFactor`
  `baseUnitShort` (per 100 g / per 1 kg / per 1 l). Sub-categories `web-od-{ddmmyyyy}` hold this and next week
  (32 + 28 items). Only web-featured items (~30/week), not the whole leaflet.
  The same API also has `/api/product-discovery/products?sortBy=relevance` (all listed products, ~60).
* robots.txt: Sitemap only. Server-rendered pages `/akcni-polozky`, `/category/vsechny-akce-99000000`
  embed the same data in `__NUXT_DATA__` (devalue format) if the API ever closes.
* Cadence: **Wednesday → Tuesday** plus Thursday–Sunday specials ("Platnost: 16. 9. – 20. 9.").

---

## 6. Tesco — SKIP

* `itesco.cz`, `www.itesco.cz/letaky`, `itesco.cz/akcni-nabidky/`, `nakup.itesco.cz/groceries/cs-CZ/` → **403 Akamai
  ("Access Denied", Reference #18…)** for our UA; `robots.txt` answered 200 only to a browser UA (diagnostic
  request only – production must not spoof). robots.txt lists sitemaps
  `https://nakup.itesco.cz/sitemaps/cs-CZ/groceries/promotions-index.xml` and disallows `/shop/*-*/*&offset=*`.
* `www.tesco.cz` is an unrelated hardware company ("Servery TESCO").
* Cadence: Wednesday → Tuesday (from kupi validity rows: "st 16. 9. – út 22. 9.").
* Use **kupi.cz** (`/slevy/tesco?f=…`, shop id 1).

---

## 7. Globus — USE (primary)

* `https://www.globus.cz/letaky` → 404; offers live at `https://www.globus.cz/globus/hypermarket/akcni-nabidka`
  (Nuxt; `__NUXT_DATA__` contains `ProductListing` with 24 items of `totalItems: 874`).
* The Nuxt app proxies GSOA (`gapi.globus.cz/ProductCatalog/2/...`) through `www.globus.cz/api/v1/gsoa/*`
  (endpoint list read from `/_nuxt/DD85NY-A.js`; action-offer service in `/_nuxt/DXIMJJgJ.js`, `DRhyS1bK.js`):
  ```
  GET https://www.globus.cz/api/v1/gsoa/actionOffers/houses/4005/actionProductsCatalog?page=0&pageSize=100
  ```
  → `{"products":[…], "paginationShowMore":true, "totalCount":874}`; page 0..8 (page 8 → 65 items, showMore=false).
  Optional query: `filter` (categoryId), `filter-productsCatalog`, `orderBy`, `useRebatesVersion`, `listedProductOnly`.
  Item (only relevant fields):
  ```
  {"name":"Kuřecí prsní řízky chlazené","regulatedName":"…","sellUnitSizeText":"500g"|"0,5 l"|null,
   "unitId":"kg"|"g"|"ml"|"ks","unitAmount":1,"ean":["…"],"vanr":"02384733000",
   "productCategories":["cls_czr_poultry_breast","cls_czr_meat_and_fish",…],
   "productInHouse":{"actualPrice":99.9,"originalPrice":222.9,"discountPercentage":55,
      "priceValidFrom":"2026-09-08T00:00:00.000+02:00","priceValidTo":"2026-09-15T23:59:59.000+02:00",
      "comparisonPrice":13.98,"comparisonSaleUnitSizeText":"100,00 g",
      "baseComparisonPrice":139.8,"baseComparisonSaleUnitSizeText":"1 kg",
      "houseNumber":4005,"priceTagId":"11","blickPunkt":true,"bonusProgramPrice":null,"availability":"A"}}
  ```
  `baseComparisonPrice` + `baseComparisonSaleUnitSizeText` ("1 kg", "1 l", "1 ks") give czkPerKg directly.
  `originalPrice=null` → not a discount (permanent price, e.g. Pilsner). `priceValidTo 9999-12-31` = permanent low price.
* House 4005 = Praha-Čakovice (default when no store cookie). Other house numbers are in the Storyblok
  hypermarket list in the page state (`content.gsoaId`); prices differ slightly per house – one house is enough.
* Also available: `/api/v1/gsoa/productCatalog/houses/4005/filteredProducts?page=N&pageSize=100` (whole assortment
  with regular prices, `totalCount` not exposed), `/products/{vanr}`, `/products/ean-{ean}`.
* robots.txt: allows `/globus/hypermarket/akcni-nabidka` (only `/*/p/` product detail pages and per-branch
  copies are disallowed). No API path mentioned. Cloudflare in front, no challenge for our UA.
* Cadence: weekly **Wednesday → Tuesday** (09.09–15.09) + two-week promos **Tuesday → Tuesday** (08.09–22.09);
  next-week items were not yet in the feed on Tuesday evening → run Wednesday ≥ 06:00 CEST.

---

## 8. kupi.cz — FALLBACK (all chains)

### Access & policy
* `https://www.kupi.cz/robots.txt`: for `User-agent: *` the search `/hledej?f=` **is allowed** except
  `*website` spam, `/hledej?*page=` (pagination) and `?ord=` sorting; `/slevy/{slug}` allowed; internal XHR
  (`/get-slevy`, `/get-letaky`, `/get-akce`) disallowed; `/letak/standalone/*` disallowed.
  There is an explicit `User-agent: AI  Disallow: /` block – our UA is `Vareska-deals`, but this signals
  the operator's stance; keep volume minimal (≤ 150 requests/day, 1 rps, `From:` header optional) and cache.
* Cloudflare (`cf-ray …-PRG`), no challenge for our UA from CZ. US runner: untested → the provider must treat a
  403/503 HTML with `challenge-platform` as "blocked, skip" and never retry with a browser UA.
* Page size: 0.2–1.7 MB (search page for "sýr" = 1.67 MB, 18 products × all stores). Prefer specific terms.

### URL patterns
* All stores: `https://www.kupi.cz/hledej?f={urlencoded term}` (18 products/page, first page only).
* One store: `https://www.kupi.cz/slevy/{albert|lidl|kaufland|tesco|billa|penny-market|globus}?f={term}` (~200 kB).
* Product detail: `/sleva/{product-slug}` (all current offers of one product; not needed).

### Parsing spec (verified on 8 queries, 400+ rows)
Product group = `<div class="group_discounts …" data-page="1">`, inside:
* `div.product--wrap[data-product-id]` → kupi product id (`pid`).
* `div.product_name h2 > a > strong` → **title** (e.g. "Máslo Jihočeské Madeta"); `h2 span.nowrap > span` → **pack**
  ("250 g", "1 l", "0.36 kg", may be empty).
* `div.avg_price > span` → "běžná cena" (average regular price, CZK, comma decimal).
* Two sections per product: `promo_discounts recommended_discounts` ("Doporučené akce") and plain `promo_discounts`
  ("Akce dle ceny") – **the same `discount_row` appears in both → dedupe on `data-discount`**.
* Offer row = `div.discount_row[data-discount][data-shop][data-key][data-position]`:
  * `data-shop` ids: **Tesco 1, Albert 3, Kaufland 4, BILLA 5, Lidl 6, Penny Market 7, Globus 27**
    (others: Košík 308, JIP 6?, Hruška 38, FLOP 235, FLOP TOP 3510, CBA 257, Ratio, Makro, Teta 33 → ignore).
    `discounts_shop_name span` text: "Albert", "Albert hypermarket", "Albert supermarket", "BILLA", "Penny Market" …
  * `strong.discount_price_value` → "39,90 Kč" (promo price for the pack).
  * `div.discount_amount` → "/ 250 g" | "/ 1 kg" | "/ 0.25 kg" | "/ 1 ks" (pack the price refers to).
  * `div.discount_percentage` → "–27 %" (optional; en dash).
  * `span.price_per_unit` → "15,96 Kč / 100 g" | "10,80 Kč / 1 kg" | "8,90 Kč / 1 l" → **czkPerKg source**:
    `100 g`→×10, `1 kg`→×1, `1 l`/`100 ml` → treat as kg (or use catalog `densityGPerMl`), `1 ks` → divide by
    catalog `unitGrams.ks`.
  * `div.discounts_validity` text (whitespace-collapsed), class `valid_discount` when currently valid; `div.discounts_price`
    gets class `price_future_discount` for upcoming offers. Observed variants and mapping (today = run date, Prague):
    | text | validFrom | validTo |
    |---|---|---|
    | `dnes končí` | today | today |
    | `zítra končí` | today | today+1 |
    | `aktuální` | today | store's current leaflet end (Wed–Tue chains: next Tuesday; Lidl: next Sunday) |
    | `st 16. 9. – út 22. 9.` | 16.9 | 22.9 (year = nearest, roll over in Dec/Jan) |
    | `platí do úterý 29. 9.` | today | 29.9 |
    | `pá 18. 9. – ne 20. 9.`, `so 19. 9. – ne 20. 9.`, `po 21. 9. – út 22. 9.` | as given | as given |
    Generic regex: `(\d{1,2})\.\s*(\d{1,2})\.` (0, 1 or 2 hits); "do" → validTo only; two hits → from–to.
  * `div.discount_note` → "max 5 ks/osoba/den", "vybrané druhy"; `div.discounts_club` → "Platí pro členy klubu"
    (club/loyalty price – keep, flag `club=true`; the Flutter app has no loyalty flag, so either accept or skip).
  * `a.btn_link_leaflet[href="/letak/{store}-letak-…?page=N&map=…"]` → leaflet page reference (provenance).
  * `a.btn_list_add[data-price="24.9"][data-product="Dýně máslová"][data-shop="4"]` → clean numeric price (float).
* Search semantics: substring/lemmatised over product names → "máslo" also returns "Dýně máslová",
  "Arašídové máslo", "Pomazánkové máslo". The ingredient matcher (mirror of `text_normalizer.dart`: fold →
  tokens ≥ 2 chars → suffix stem, longest-first list, never below 4 chars) must require that the *ingredient's*
  stemmed tokens all occur in the title and reject titles containing any token of a **negative list** per
  ingredient (e.g. maslo: `arasid`, `pomazank`, `dyn`, `kakaov`, `oriskov`; mleko: `kokosov`, `kondenz`, `susen`;
  kureci prsa: `salat`, `sunk`, `salam`). Prefer rows whose `pack` unit is g/kg/l over `ks` when the catalog
  ingredient is a bulk good.
* Empty result: page renders with zero `group_discounts` (no "nenalezeno" marker) → treat as no data.

---

## 9. akcniceny.cz — SKIP (reserve)

* robots.txt: `Crawl-delay: 1`, disallows `/admin/`, `/seznam/a/`, `/redir`, `/*/vytisknout`, `/php/sapi.php*`.
  Search form: `GET https://www.akcniceny.cz/hledej?s={term}` (param `s`; `?q=` is ignored, `/vyhledavani/` 404).
* Today it answers 200 to our UA (the earlier "blocks non-browser clients" observation did not reproduce);
  results are product cards (`itemtype=schema.org/Product`, `itemprop=name/url`, price range
  "39,90 - 44,90 Kč", store logos) – per-store prices and validity are only on `/akce/{slug}/` (second request
  per product). Fewer matches than kupi (6 vs 44 rows for "máslo"). Keep as a manual reserve only.

---

## 10. Geo / bot-protection notes for GitHub Actions (US IPs)

| Host | Edge | Observed from CZ | Expected from US runner |
|---|---|---|---|
| www.lidl.cz, endpoints.leaflets.schwarz | Myra Security (myracloud) | 200 | probably 200 (global CDN); verify in first CI run |
| www.kaufland.cz | Cloudflare **managed challenge** | 403 | 403 – never fetch |
| www.globus.cz (`/api/v1/gsoa/*`) | Cloudflare | 200 | 200 or 403 challenge for DC ASN – detect `cf-mitigated: challenge` header and skip |
| www.kupi.cz | Cloudflare | 200 | same as above |
| itesco.cz, nakup.itesco.cz | Akamai | 403 (UA-based) | 403 |
| www.albert.cz, letaky.albert.cz, view.publitas.com | Apache / nginx | 200 | 200 |
| www.penny.cz, www.billa.cz | istio-envoy (GCP) | 200 | 200 |
| files.rewe.co.at | IIS | 200 | 200 |

Mitigation if Cloudflare hosts start challenging the runner: run the fetch on a self-hosted runner / cron in CZ,
or commit results from a local run; never add stealth headers or challenge solvers.

---

## 11. Cadence summary

| Chain | Leaflet validity | New leaflet visible online | Notes |
|---|---|---|---|
| Lidl | Mon–Wed and Thu–Sun (+ Mon–Sun non-food) | Friday for next week | product API carries future ranges |
| Kaufland | Wed–Tue | by Tuesday (both weeks listed) | monthly "Wrapper" catalogue too |
| Albert | Wed–Tue (HM + SM variants) | Monday morning | Bio/Wine catalogues 2–4 weeks |
| Billa | Wed–Tue (Velký + Malý leták) | Monday | club prices |
| Penny | Wed–Tue + Thu–Sun specials | Tuesday | web API only 30 items/week |
| Globus | Wed–Tue weekly + Tue–Tue biweekly | Wednesday morning | 874 items, one house |
| Tesco | Wed–Tue | – | blocked |

---

## 12. kupi.cz ingredient search terms (150)

Terms are nominative singular Czech as kupi indexes them; `ingredientId` refers to `assets/data/ingredients.json`.
Machine-readable copy: `docs/kupi_terms.json` (`[{"id":…, "q":…, "neg":[…]}]`).

| # | ingredientId | query | negative tokens (folded stems) |
|---|---|---|---|
| 1 | maslo | máslo | arasid, pomazank, dyn, kakaov, orisk, mandl |
| 2 | mleko_15 | mléko polotučné | kokos, kondenz, susen, sojov, ovesn |
| 3 | mleko_35 | mléko plnotučné | kokos, kondenz, susen |
| 4 | smetana_33 | smetana ke šlehání | rostlinn |
| 5 | smetana_12 | smetana na vaření | rostlinn |
| 6 | smetana_zakysana | zakysaná smetana | |
| 7 | jogurt_bily | bílý jogurt | reck |
| 8 | jogurt_recky | řecký jogurt | |
| 9 | tvaroh | tvaroh | dezert, tycink |
| 10 | kefir | kefír | |
| 11 | podmasli | podmáslí | |
| 12 | eidam | eidam | |
| 13 | gouda | gouda | |
| 14 | emental | ementál | |
| 15 | cedar | čedar | |
| 16 | hermelin | hermelín | |
| 17 | niva | niva | |
| 18 | mozzarella | mozzarella | tycink |
| 19 | balkansky_syr | balkánský sýr | |
| 20 | feta | feta | |
| 21 | parmazan | parmazán | |
| 22 | mascarpone | mascarpone | |
| 23 | ricotta | ricotta | |
| 24 | smetanovy_syr | smetanový sýr | |
| 25 | halloumi | halloumi | |
| 26 | kozi_syr | kozí sýr | |
| 27 | vejce | vejce | cokolad, kinder |
| 28 | margarin | margarín | |
| 29 | sadlo | sádlo | |
| 30 | kureci_prsa | kuřecí prsa | salat, sunk, salam, uzen |
| 31 | kureci_stehna | kuřecí stehna | |
| 32 | kureci_stehenni_rizky | kuřecí stehenní řízky | |
| 33 | kureci_kridla | kuřecí křídla | |
| 34 | kure_cele | kuře | prs, stehn, kridl, jatr, mlet, salat, polevk |
| 35 | kureci_jatra | kuřecí játra | |
| 36 | mlete_kureci | mleté kuřecí | |
| 37 | kruti_prsa | krůtí prsa | sunk |
| 38 | kachna_cela | kachna | prs, stehn |
| 39 | kachni_stehna | kachní stehna | |
| 40 | veprova_kyta | vepřová kýta | |
| 41 | veprova_krkovice | vepřová krkovice | |
| 42 | veprova_plec | vepřová plec | |
| 43 | veprova_pecene | vepřová pečeně | |
| 44 | veprova_kotleta | vepřová kotleta | |
| 45 | veprova_panenka | vepřová panenka | |
| 46 | veprovy_bucek | vepřový bůček | |
| 47 | veprova_zebra | vepřová žebra | |
| 48 | veprove_koleno | vepřové koleno | |
| 49 | mlete_veprove | mleté vepřové | |
| 50 | mlete_hovezi | mleté hovězí | |
| 51 | mlete_maso_mix | mleté maso mix | |
| 52 | hovezi_zadni | hovězí zadní | |
| 53 | hovezi_kliska | hovězí kližka | |
| 54 | hovezi_rostenec | hovězí roštěná | |
| 55 | hovezi_svickova | hovězí svíčková | omack |
| 56 | hovezi_zebra | hovězí žebra | |
| 57 | teleci_maso | telecí | |
| 58 | jehneci_kyta | jehněčí | |
| 59 | kralik | králík | |
| 60 | sunka | šunka | salat, pomazank |
| 61 | anglicka_slanina | anglická slanina | |
| 62 | spek | špek | |
| 63 | klobasa | klobása | |
| 64 | parky | párky | |
| 65 | spekacek | špekáčky | |
| 66 | uzene_maso | uzené maso | |
| 67 | salam_gothaj | gothajský salám | |
| 68 | losos | losos | pomazank, salat |
| 69 | treska | treska | |
| 70 | pstruh | pstruh | |
| 71 | kapr | kapr | |
| 72 | makrela | makrela | |
| 73 | rybi_file | rybí filé | |
| 74 | tunak_konzerva | tuňák | salat, pomazank |
| 75 | sardinky_konzerva | sardinky | |
| 76 | krevety | krevety | |
| 77 | brambory | brambory | kase, salat, knedl, lupinky, chips, hranolk |
| 78 | batat | batáty | |
| 79 | cibule | cibule | jarn, cerven, smazen, susen |
| 80 | cibule_cervena | červená cibule | |
| 81 | cibule_jarni | jarní cibulka | |
| 82 | cesnek | česnek | susen, medvedi |
| 83 | mrkev | mrkev | |
| 84 | petrzel_koren | petržel | nat |
| 85 | celer_bulva | celer | rapik |
| 86 | porek | pórek | |
| 87 | rajcata | rajčata | cherry, susen, sterilov, loupan, protlak |
| 88 | rajcata_cherry | cherry rajčata | |
| 89 | okurka_salatova | okurka | kysel, sterilov, naklad |
| 90 | okurky_kysele | kyselé okurky | |
| 91 | paprika_cervena | paprika | mlet, plnen, sterilov, uzen |
| 92 | cuketa | cuketa | |
| 93 | lilek | lilek | |
| 94 | brokolice | brokolice | |
| 95 | kvetak | květák | |
| 96 | zeli_bile | zelí | kysan, cerven, pekingsk |
| 97 | zeli_cervene | červené zelí | |
| 98 | kysane_zeli | kysané zelí | |
| 99 | zeli_pekingske | pekingské zelí | |
| 100 | kapusta | kapusta | |
| 101 | kedlubna | kedlubna | |
| 102 | cervena_repa | červená řepa | |
| 103 | dyne_hokkaido | dýně hokkaido | |
| 104 | spenat_cerstvy | špenát | |
| 105 | salat_ledovy | ledový salát | |
| 106 | salat_hlavkovy | hlávkový salát | |
| 107 | rukola | rukola | |
| 108 | zampiony | žampiony | |
| 109 | redkvicky | ředkvičky | |
| 110 | zazvor | zázvor | mlet |
| 111 | avokado | avokádo | |
| 112 | citron | citrony | |
| 113 | limetka | limetky | |
| 114 | jablko | jablka | susen, mus, stav |
| 115 | hruska | hrušky | |
| 116 | banan | banány | |
| 117 | pomeranc | pomeranče | |
| 118 | hrozny | hroznové víno | |
| 119 | jahody | jahody | mrazen, dzem |
| 120 | boruvky | borůvky | |
| 121 | maliny | maliny | |
| 122 | svestky | švestky | susen, povidl |
| 123 | broskve | broskve | kompot |
| 124 | merunky | meruňky | susen, dzem |
| 125 | meloun_vodni | meloun | |
| 126 | mango | mango | |
| 127 | kiwi | kiwi | |
| 128 | ananas | ananas | kompot |
| 129 | mouka_hladka | hladká mouka | |
| 130 | mouka_polohruba | polohrubá mouka | |
| 131 | mouka_hruba | hrubá mouka | |
| 132 | cukr | cukr krystal | |
| 133 | cukr_moucka | moučkový cukr | |
| 134 | ryze_dlouhozrnna | rýže dlouhozrnná | |
| 135 | ryze_jasminova | jasmínová rýže | |
| 136 | ryze_basmati | basmati | |
| 137 | spagety | špagety | |
| 138 | penne | penne | |
| 139 | fusilli | fusilli | |
| 140 | ovesne_vlocky | ovesné vločky | |
| 141 | kuskus | kuskus | |
| 142 | bulgur | bulgur | |
| 143 | cocka_cervena | čočka | |
| 144 | cizrna_sterilovana | cizrna | |
| 145 | fazole_cervene_sterilovane | fazole | zelen, lusk |
| 146 | hrasek_mrazeny | hrášek | |
| 147 | olej_slunecnicovy | slunečnicový olej | |
| 148 | olej_repkovy | řepkový olej | |
| 149 | olej_olivovy | olivový olej | |
| 150 | med | med | medov, pernik |
| 151 | chleb | chléb | toustov |
| 152 | rohlik | rohlík | |
| 153 | listove_teste | listové těsto | |
| 154 | strouhanka | strouhanka | |
| 155 | kecup | kečup | |
| 156 | horcice | hořčice | |
| 157 | majoneza | majonéza | |
| 158 | rajcatovy_protlak | rajčatový protlak | |
| 159 | passata | passata | |
| 160 | kokosove_mleko | kokosové mléko | |
| 161 | sojova_omacka | sójová omáčka | |
| 162 | bujon_kostka | bujón | |
| 163 | vlasske_orechy | vlašské ořechy | |
| 164 | mandle | mandle | |
| 165 | arasidy | arašídy | maslo |
| 166 | tofu | tofu | |
| 167 | cokolada_horka | hořká čokoláda | |
| 168 | kakao | kakao | |
| 169 | kava_mleta | mletá káva | |
| 170 | drozdi_cerstve | droždí | |

(170 terms; the first 150 are the priority set – meat, dairy, produce, staples. Terms 151–170 are cheap extras.)

---

## 13. Request examples (curl)

```sh
UA='Vareska-deals/1.0 (+https://github.com/stanislavmudra-hue/vareska-data)'
# Globus, 9 pages
curl -sA "$UA" 'https://www.globus.cz/api/v1/gsoa/actionOffers/houses/4005/actionProductsCatalog?page=0&pageSize=100'
# Lidl, one food sub-category
curl -sA "$UA" -H 'Accept: application/json' 'https://www.lidl.cz/q/api/category/h/syry-mlecne-vyrobky-a-vejce/h10095761?assortment=CZ&locale=cs_CZ&version=v2.0.0&pageId=10068374%2F10095761&fetchsize=200'
# Lidl flyer list (validity dates, PDFs)
curl -sA "$UA" 'https://endpoints.leaflets.schwarz/v4/overview?client_locale=lidl/cs-CZ&region_id=0'
# Kaufland flyer list (validity dates only)
curl -sA "$UA" 'https://endpoints.leaflets.schwarz/v4/overview?client_locale=kaufland/cs-CZ&region_id=1000'
# Penny web promotions
curl -sA "$UA" 'https://www.penny.cz/api/product-discovery/categories/vsechny-akce-99000000/products?page=0&pageSize=100'
# Albert leaflet list (parse __NEXT_DATA__ → apolloState → Leaflet:*) then Publitas text
curl -sA "$UA" 'https://www.albert.cz/aktualni-letaky'
curl -sA "$UA" 'https://letaky.albert.cz/38hm_akcni_letak/spreads.json?page=1'
# Billa
curl -sA "$UA" 'https://www.billa.cz/akcni-letaky'
curl -sA "$UA" 'https://view.publitas.com/billa-cz/velky-letak-16-9-22-9-2026/spreads.json?page=1'
# kupi fallback
curl -sA "$UA" 'https://www.kupi.cz/hledej?f=m%C3%A1slo'
curl -sA "$UA" 'https://www.kupi.cz/slevy/kaufland?f=m%C3%A1slo'
```

## 14. Open items for the implementer
1. First CI run must log HTTP status + `server`/`cf-mitigated` headers per host to confirm US-IP behaviour.
2. Lidl category ids: read the facet tree from the root food category each run; fail soft per category.
3. Publitas text parsers (Albert/Billa) need a golden-file test on the saved `spreads.json` samples.
4. Kupi matcher: implement the Python mirror of `text_normalizer.dart` (`fold`, `tokens`, `stem`, `tokenMatches`)
   and the per-term negative lists from `docs/kupi_terms.json`.
5. Respect `Crawl-delay`/1 rps, exponential backoff on 429/5xx, and stop the whole provider on the first 403.

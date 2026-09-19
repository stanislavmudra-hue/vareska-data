"""Markets: per-country stores/currency, multilingual parsing, Lidl provider per
market, mapper languages, builder/validator schema v2, per-market report."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

import pytest

from conftest import TODAY, FakeHttp, fixture_text
from pipeline import markets, textnorm
from pipeline.providers import base, lidl, registry

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "pipeline"))

import build_prices as bp  # noqa: E402
import mapper as M  # noqa: E402
import report  # noqa: E402
import validate  # noqa: E402


# ---- markets ---------------------------------------------------------------
def test_market_table_matches_the_app():
    assert markets.codes() == ["cz", "sk", "pl", "de", "at"]
    assert markets.stores_of("cz") == ("albert", "lidl", "kaufland", "tesco", "billa", "penny", "globus")
    assert markets.stores_of("sk") == ("tesco", "lidl", "kaufland", "billa", "coopJednota", "terno", "fresh")
    assert markets.stores_of("pl") == ("biedronka", "lidl", "kaufland", "auchan", "carrefour", "netto", "dino", "aldi", "zabka")
    assert markets.stores_of("de") == ("aldiNord", "aldiSued", "lidl", "kaufland", "edeka", "rewe", "penny", "netto", "norma")
    assert markets.stores_of("at") == ("billa", "spar", "interspar", "hofer", "lidl", "penny", "mpreis")
    assert [markets.get(c).currency for c in markets.codes()] == ["CZK", "EUR", "PLN", "EUR", "EUR"]
    assert [markets.get(c).lang for c in markets.codes()] == ["cs", "sk", "pl", "de", "de"]
    assert markets.get("SK").fx == 0.041 and markets.get("pl").fx == 0.17 and markets.get("cz").fx == 1.0
    with pytest.raises(KeyError):
        markets.get("hu")
    assert markets.parse_selection("all") == markets.codes() and markets.parse_selection(None) == markets.codes()
    assert markets.parse_selection("sk, CZ,sk") == ["sk", "cz"]


def test_category_fallback_is_czech_times_fx():
    fb = markets.category_fallback({"dairy": 120, "meat": 240}, "sk")
    assert fb == {"dairy": 4.92, "meat": 9.84}
    assert markets.category_fallback({"dairy": 120}, "pl") == {"dairy": 20.4}
    assert markets.category_fallback({"dairy": 120}, "cz") == {"dairy": 120}


def test_market_paths():
    cz, sk = markets.paths("cz"), markets.paths("sk")
    assert cz["offers"].endswith(os.path.join("out", "offers.json"))
    assert cz["prices_v1"].endswith(os.path.join("docs", "prices.json"))
    assert cz["prices"].endswith(os.path.join("docs", "prices", "cz.json"))
    assert cz["last_seen"].endswith(os.path.join("out", "last_seen.json"))
    assert sk["prices_v1"] is None
    assert sk["offers"].endswith(os.path.join("out", "sk", "offers.json"))
    assert sk["prices"].endswith(os.path.join("docs", "prices", "sk.json"))
    assert sk["last_seen"].endswith(os.path.join("out", "last_seen_sk.json"))
    assert sk["panel_dir"].endswith(os.path.join("docs", "data", "sk"))


# ---- text folding for sk / pl / de ------------------------------------------
def test_fold_handles_polish_german_slovak_letters():
    assert textnorm.fold("Łosoś ząb gęś śliwka żółć źdźbło ćma koń") == "losos zab ges sliwka zolc zdzblo cma kon"
    assert textnorm.fold("Käse Öl Müsli Straße") == "kase ol musli strasse"
    assert textnorm.fold("Ľadový šalát, môj ŕ ä") == "ladovy salat moj r a"
    assert M.fold("Masło ekstra 82%") == "maslo ekstra 82"


def test_stemmer_per_language_never_below_four_chars():
    assert M.stem("ziemniaki", "pl") == "ziemnia"
    assert M.stem("kartoffeln", "de") == "kartoffel"
    assert M.stem("masl", "pl") == "masl"
    assert M.stem("brambory", "sk") == "brambor"


# ---- multilingual pack / unit-price parsing ----------------------------------
@pytest.mark.parametrize("text,expected", [
    ("1 kg = 174,75 Kč", 174.75), ("100 g = 0,16", 1.6), ("750 ml 1 L = 13,32", 13.32),
    ("Je 250 g (1 kg = 13.96)", 13.96), ("2,49/100 g", 24.9), ("15,96 Kč / 100 g", 159.6),
    ("100 g 1 kg = 16,90 * cena przed obniżką: 2,49/100 g, 1 kg = 24,90", 16.9),
    ("1 kg * cena przed obniżką: 1 kg = 14,99", None),          # only the old price is per kg
    ("Ab 3 Stk. je 500 g (1 kg = 2.64)", 2.64), ("Je 12x 0,33 l (0,5 l = 1.26)", 2.52),
    ("Dwusztuk, 2 x 1 L 1 L = 4,99 + kaucja 1,00 zł", 4.99), ("5 x 50 g = 250 g", None),
    ("Je 125/150 g (1 kg = 18.32/15.27)", 18.32), ("1 kus = 0,43", None), ("0,33 €/l", 0.33),
])
def test_parse_unit_price_any(text, expected):
    got = base.parse_unit_price_any(text)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=0.01)


def test_parse_unit_price_any_pieces_for_eggs_only():
    assert base.parse_unit_price_any("1 szt. = 0,89", "Jaja z chowu ściółkowego M") == pytest.approx(0.89 / 0.055)
    assert base.parse_unit_price_any("1 Stk. = 0.22", "Eier aus Bodenhaltung") == pytest.approx(4.0)
    assert base.parse_unit_price_any("1 Stk. = 0.22", "Semmel") is None


@pytest.mark.parametrize("text,expected", [
    ("500 g balenie", (500.0, "g")), ("750 ml 1 L = 13,32", (750.0, "ml")), ("Je 250 g (1 kg = 13.96)", (250.0, "g")),
    ("Ab 3 Stk. je 500 g (1 kg = 2.64)", (500.0, "g")), ("Je 12x 0,33 l (0,5 l = 1.26)", (3.96, "l")),
    ("Dwusztuk, 2 x 1 L 1 L = 4,99 + kaucja 1,00 zł", (2.0, "l")), ("5 x 50 g = 250 g", (250.0, "g")),
    ("3 szt. 1 szt. = 2,33 * cena przed obniżką: 9,99/opak.", (3.0, "ks")), ("3 kusy v balení", (3.0, "ks")),
    ("Je 12 Stück (1 Stk. = 0.22)", (12.0, "ks")), ("Bei 3 Stk. je Stück", (1.0, "ks")), ("kus", (1.0, "ks")),
    ("Je kg", (1.0, "kg")), ("cena za 1 kg", (1.0, "kg")), ("cena za 100 g", (100.0, "g")),
    ("Je 125/150 g (1 kg = 18.32/15.27)", (125.0, "g")), ("1 kg * cena przed obniżką: 1 kg = 14,99", (1.0, "kg")),
    ("1 pęczek", (None, None)), ("", (None, None)), ("1 kg = 0,54", (None, None)),
])
def test_parse_pack_any(text, expected):
    got = base.parse_pack_any(text)
    if expected[0] is None:
        assert got == expected
    else:
        assert got[1] == expected[1] and got[0] == pytest.approx(expected[0])


def test_offer_market_and_currency():
    o = base.Offer(store="lidl", title="Masło", price_czk=7.99, unit="g", quantity=200, market="pl")
    assert (o.currency, o.market, o.price_per_kg) == ("PLN", "pl", pytest.approx(39.95))
    assert o.to_dict()["perKg"] == o.price_per_kg
    with pytest.raises(ValueError):
        base.Offer(store="globus", title="x", price_czk=1, market="sk")   # Globus is not a Slovak store
    base.Offer(store="coopJednota", title="x", price_czk=1, market="sk")
    assert base.is_egg_title("Jaja z wolnego wybiegu") and base.is_egg_title("Eier aus Bodenhaltung") \
        and base.is_egg_title("Vajcia M") and not base.is_egg_title("Semmel")


# ---- Lidl provider per market ---------------------------------------------------
def test_lidl_sites_share_the_platform():
    for code in markets.codes():
        site = lidl.site_for(code)
        assert site.base == markets.get(code).lidl_domain and site.root_path.endswith("/s10068374")
        assert f"assortment={site.assortment}&locale={site.locale}" in lidl.query_for(site)
    assert lidl.site_for("cz").root_path == "/c/potraviny-a-napoje/s10068374"


def _lidl(market: str, fixture: str):
    http = FakeHttp([(lambda u: "s10068374" in u, fixture)])
    p = lidl.LidlProvider(http=http, today=dt.date(2026, 9, 19), market=market)
    offers = p.fetch()
    return p, http, {o.title: o for o in offers}


def test_lidl_sk_parses_euro_unit_prices_and_periods():
    p, http, by = _lidl("sk", "lidl_sk_root.json")
    assert p.name == "lidl_sk" and all("lidl.sk/q/api/category" in u and "assortment=SK&locale=sk_SK" in u for u in http.urls)
    assert all("pageId" not in u for u in http.urls)
    zem = by["Konzumné zemiaky ružové"]
    assert (zem.market, zem.currency, zem.store, zem.source) == ("sk", "EUR", "lidl", "lidl_sk")
    assert (zem.unit, zem.quantity, zem.price_czk, zem.price_per_kg) == ("kg", 5.0, 2.69, 0.54)
    assert (zem.valid_from, zem.valid_to, zem.is_promo) == ("2026-09-21", "2026-09-27", True)
    krev = by["Biele tigrie krevety varené lúpané"]
    assert (krev.unit, krev.quantity, krev.price_per_kg, krev.original_price_czk) == ("g", 220.0, 18.1, 4.99)
    # a bakery item listed for six weeks: the period containing today (19.9.) wins
    kai = by["Kaizerka"]
    assert (kai.valid_from, kai.valid_to, kai.price_per_kg) == ("2026-09-14", "2026-09-20", 1.6)
    assert (by["Kivi"].unit, by["Kivi"].quantity, by["Kivi"].price_per_kg) == ("ks", 1.0, None)
    assert (by["Červené jablká Gala"].unit, by["Červené jablká Gala"].price_per_kg) == ("kg", 0.99)
    assert (by["Kačacie stehná"].unit, by["Kačacie stehná"].quantity, by["Kačacie stehná"].price_per_kg) == ("g", 100.0, 7.9)
    assert by["Bánovecký jogurt"].valid_to is None and by["Bánovecký jogurt"].is_promo is False


def test_lidl_pl_parses_zloty_packaging_texts():
    p, http, by = _lidl("pl", "lidl_pl_root.json")
    assert p.name == "lidl_pl" and all("assortment=PL&locale=pl_PL" in u for u in http.urls)
    assert "PILOS Masło ekstra 82%" not in by            # no price -> dropped
    chleb = by["Chleb górski z zakwasem"]
    assert (chleb.currency, chleb.unit, chleb.quantity, chleb.price_per_kg, chleb.original_price_czk) == ("PLN", "g", 380.0, 5.24, 4.29)
    assert (chleb.valid_from, chleb.valid_to) == ("2026-09-14", "2026-09-19")
    ziem = by["Polskie ziemniaki jadalne, luzem"]
    assert (ziem.unit, ziem.quantity, ziem.price_per_kg) == ("kg", 1.0, 0.99)   # old "1 kg = 1,99" ignored
    cola = by["Coca-Cola, Coca-Cola Zero, Fanta lub Sprite"]
    assert (cola.unit, cola.quantity, cola.price_per_kg, cola.is_promo) == ("l", 2.0, 4.99, True)
    assert (by["Imbir, luzem"].unit, by["Imbir, luzem"].quantity, by["Imbir, luzem"].price_per_kg) == ("g", 100.0, 16.9)
    mar = by["Marakuja"]
    assert (mar.unit, mar.quantity, mar.price_per_kg) == ("ks", 3.0, None)
    assert (mar.valid_from, mar.valid_to) == ("2026-09-17", "2026-09-19")


def test_lidl_at_parses_german_unit_strings():
    p, http, by = _lidl("at", "lidl_at_root.json")
    assert p.name == "lidl_at" and all("assortment=AT&locale=de_AT" in u for u in http.urls)
    assert (by["Schalotten"].unit, by["Schalotten"].quantity, by["Schalotten"].price_per_kg) == ("g", 500.0, 1.98)
    assert (by["Käsesemmel"].unit, by["Käsesemmel"].quantity, by["Käsesemmel"].price_czk) == ("ks", 1.0, 0.52)
    assert (by["Lachsfilet"].unit, by["Lachsfilet"].quantity, by["Lachsfilet"].price_per_kg) == ("g", 1000.0, 16.49)
    rb = by["WIESENTALER Frischer Puten Rollbraten"]
    assert (rb.unit, rb.quantity, rb.price_per_kg, rb.original_price_czk) == ("kg", 1.0, 6.99, 8.99)
    beer = by["VILLACHER Märzen"]
    assert (beer.unit, beer.quantity, beer.price_per_kg) == ("l", 10.0, 1.48)
    assert all(o.currency == "EUR" and o.market == "at" for o in by.values())
    assert by["Schalotten"].valid_to == "2026-09-19"


def test_registry_carries_a_market_per_provider():
    assert registry.for_market("cz") == list(registry.DEFAULT_ORDER)
    assert registry.for_market("sk") == ["lidl_sk"] and registry.for_market("at") == ["lidl_at"]
    assert {registry.market_of(n) for n in registry.PROVIDERS} == set(markets.codes())
    assert registry.market_of("lidl_pl") == "pl" and registry.market_of("globus") == "cz"
    assert registry.not_fetched("sk") == ["tesco", "kaufland", "billa", "coopJednota", "terno", "fresh"]
    assert registry.not_fetched("cz") == []
    prov = registry.get("lidl_de")(http=FakeHttp([]), today=TODAY)
    assert isinstance(prov, lidl.LidlProvider) and prov.market == "de" and prov.site.max_requests == 15


# ---- mapper per market ------------------------------------------------------------
CATALOG_ROWS = [
    {"id": "maslo", "nameCs": "máslo", "aliases": ["másla"], "names": {"sk": "maslo", "en": "butter", "de": "Butter", "pl": "masło"},
     "category": "dairy", "refPriceCzkPerKg": 200},
    {"id": "kureci_prsa", "nameCs": "kuřecí prsa", "aliases": ["kuřecí prsní řízky"],
     "names": {"sk": "kuracie prsia", "en": "chicken breast", "de": "Hähnchenbrust", "pl": "pierś z kurczaka"},
     "namesPlural": {"sk": "kuracie prsia", "en": "chicken breasts", "de": "Hähnchenbrüste", "pl": "piersi z kurczaka"},
     "category": "poultry", "refPriceCzkPerKg": 180},
    {"id": "mleko_35", "nameCs": "plnotučné mléko", "aliases": ["mléko 3,5 %"],
     "names": {"sk": "plnotučné mlieko", "en": "whole milk", "de": "Vollmilch", "pl": "mleko pełnotłuste"},
     "category": "dairy", "densityGPerMl": 1.03, "refPriceCzkPerKg": 28},
    {"id": "mleko_15", "nameCs": "polotučné mléko", "aliases": ["mléko"],
     "names": {"sk": "polotučné mlieko", "en": "semi-skimmed milk", "de": "fettarme Milch", "pl": "mleko półtłuste"},
     "category": "dairy", "densityGPerMl": 1.03, "refPriceCzkPerKg": 25},
    {"id": "brambory", "nameCs": "brambory", "aliases": ["brambor"],
     "names": {"sk": "zemiaky", "en": "potatoes", "de": "Kartoffeln", "pl": "ziemniaki"},
     "category": "vegetable", "refPriceCzkPerKg": 20},
]


def test_catalog_names_per_language():
    row = CATALOG_ROWS[1]
    assert M.catalog_names(row, "cs") == ["kuřecí prsa", "kuřecí prsní řízky"]
    assert M.catalog_names(row, "pl") == ["pierś z kurczaka", "piersi z kurczaka", "kuřecí prsa"]
    assert M.catalog_names(row, "sk")[:3] == ["kuracie prsia", "kuracie prsia", "kuřecí prsa"]
    assert "kuřecí prsní řízky" in M.catalog_names(row, "sk") and "kuřecí prsní řízky" not in M.catalog_names(row, "de")
    cat = M.Catalog(CATALOG_ROWS, market="pl")
    assert cat.lang == "pl" and cat.items["maslo"].name == "masło"
    assert cat.items["maslo"].ref_price == pytest.approx(200 * 0.17)      # fx applied to the plausibility band
    assert M.Catalog(CATALOG_ROWS, market="cz").items["maslo"].ref_price == 200


def _map(market: str, title: str, price: float, pack: str, store: str = "lidl", **extra):
    cat = M.Catalog(CATALOG_ROWS, market=market)
    r = M.Mapper(cat, []).map_offer({"title": title, "store": store, "price": price, "pack": pack, **extra})
    return r


def test_mapper_matches_localized_names():
    r = _map("pl", "PILOS Masło ekstra 82%", 7.99, "200 g")
    assert (r["status"], r["ingredientId"], r["market"], r["currency"]) == ("matched", "maslo", "pl", "PLN")
    assert r["czkPerKg"] == pytest.approx(39.95) and r["ingredientName"] == "masło"
    r = _map("sk", "Kuracie prsia", 5.49, "1 kg")
    assert (r["status"], r["ingredientId"], r["czkPerKg"]) == ("matched", "kureci_prsa", 5.49)
    r = _map("de", "Frische Vollmilch 3,5 %", 1.09, "1 l")
    assert (r["status"], r["ingredientId"]) == ("matched", "mleko_35")
    assert r["czkPerKg"] == pytest.approx(1.09 / 1.03, abs=0.01)
    r = _map("at", "Kartoffeln aus Österreich", 1.99, "2 kg")
    assert (r["status"], r["ingredientId"]) == ("matched", "brambory") and r["czkPerKg"] == pytest.approx(0.995, abs=0.01)
    r = _map("pl", "Polskie ziemniaki jadalne, luzem", 0.99, "1 kg", store="biedronka")
    assert r["ingredientId"] == "brambory" and r["store"] == "biedronka"


def test_mapper_store_enum_per_market():
    assert M.normalize_store("coopJednota", markets.stores_of("sk")) == "coopJednota"
    assert M.normalize_store("COOP Jednota", markets.stores_of("sk")) == "coopJednota"
    assert M.normalize_store("Aldi Nord", markets.stores_of("de")) == "aldiNord"
    assert M.normalize_store("globus", markets.stores_of("sk")) is None
    r = _map("sk", "Maslo", 1.99, "250 g", store="globus")
    assert r["store"] is None and "unknown-store" in r["reasons"] and r["status"] != "matched"


def test_rules_are_scoped_by_market(tmp_path):
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps({"v": 1, "rules": [
        {"pattern": "sub:masło", "store": "*", "ingredientId": "ignore", "market": "pl"},
        {"pattern": "sub:maslo", "store": "*", "ingredientId": "ignore"},               # cz only
        {"pattern": "re:^x", "store": "*", "ingredientId": "ignore", "market": "*"},
    ]}), encoding="utf-8")
    assert [r.market for r in M.load_rules(str(path))] == ["pl", "cz", "*"]
    assert [r.pattern for r in M.load_rules(str(path), "pl")] == ["sub:masło", "re:^x"]
    assert [r.pattern for r in M.load_rules(str(path), "cz")] == ["sub:maslo", "re:^x"]
    cat = M.Catalog(CATALOG_ROWS, market="pl")
    r = M.Mapper(cat, M.load_rules(str(path), "pl")).map_offer({"title": "Masło ekstra", "store": "lidl", "price": 7.99, "pack": "200 g"})
    assert r["status"] == "ignored"


# ---- builder v2 ------------------------------------------------------------------------
class BuilderV2Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.catalog = os.path.join(self.root, "ingredients.json")
        bp.dump_json(self.catalog, {"v": 1, "ingredients": CATALOG_ROWS})
        self.fallback = os.path.join(self.root, "category_fallback.json")
        bp.dump_json(self.fallback, {"categoryFallbackCzkPerKg": {"dairy": 120, "vegetable": 45}})
        self.matched = os.path.join(self.root, "matched.json")
        self.v1 = os.path.join(self.root, "prices.json")
        self.v2 = os.path.join(self.root, "prices", "sk.json")
        self.last_seen = os.path.join(self.root, "last_seen_sk.json")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, items, market="sk", today=dt.date(2026, 9, 19), previous=None):
        bp.dump_json(self.matched, items)
        if previous is not None:
            bp.dump_json(self.v2, previous)
        return bp.build(self.matched, self.v1, self.last_seen, self.catalog, self.fallback,
                        build_report_path=None, today=today, market=market, prices_v2_path=self.v2)

    def test_v2_shape_currency_and_fallback(self):
        items = [
            {"ingredientId": "maslo", "store": "lidl", "czkPerKg": 7.96, "promo": False, "title": "Maslo 250 g"},
            {"ingredientId": "maslo", "store": "lidl", "czkPerKg": 7.16, "promo": True, "validTo": "2026-09-25", "title": "Maslo akcia"},
            {"ingredientId": "maslo", "store": "coopJednota", "perKg": 8.4, "promo": False, "title": "Maslo"},
            {"ingredientId": "maslo", "store": "globus", "czkPerKg": 7.0, "title": "not a Slovak store"},
            {"ingredientId": "maslo", "store": "lidl", "czkPerKg": 159.6, "title": "Czech price in EUR = outlier"},
        ]
        table, rep = self.build(items)
        self.assertFalse(os.path.exists(self.v1))          # no v1 file for sk
        self.assertEqual((table["v"], table["market"], table["currency"], table["updated"]), (2, "sk", "EUR", "2026-09-19"))
        self.assertEqual(set(table), {"v", "market", "currency", "updated", "perKg", "deals", "categoryFallbackPerKg"})
        self.assertEqual(table["perKg"], {"maslo": {"coopJednota": 8.4, "lidl": 8.0}})
        self.assertEqual(table["deals"], [{"ingredientId": "maslo", "store": "lidl", "perKg": 7.2,
                                           "validFrom": "2026-09-19", "validTo": "2026-09-25", "title": "Maslo akcia"}])
        self.assertEqual(table["categoryFallbackPerKg"]["dairy"], 4.92)
        self.assertEqual(table["categoryFallbackPerKg"]["vegetable"], round(45 * 0.041, 2))
        self.assertEqual(table["categoryFallbackPerKg"]["meat"], round(240 * 0.041, 2))
        self.assertEqual(rep["skipped"], {"bad_store": 1, "outlier": 1})
        self.assertEqual(rep["storesMissing"], ["tesco", "kaufland", "billa", "terno", "fresh"])
        self.assertEqual(json.load(open(self.v2, encoding="utf-8"))["market"], "sk")

    def test_empty_market_is_valid_and_carry_forward_reads_v2(self):
        table, rep = self.build([])
        self.assertEqual((table["perKg"], table["deals"]), ({}, []))
        prev = {"v": 2, "market": "sk", "currency": "EUR", "updated": "2026-09-10",
                "perKg": {"maslo": {"lidl": 7.9}, "brambory": {"lidl": 0.5}},
                "deals": [{"ingredientId": "maslo", "store": "lidl", "perKg": 7.2, "validFrom": "2026-09-10",
                           "validTo": "2026-09-30", "title": "Maslo akcia"}],
                "categoryFallbackPerKg": {}}
        bp.dump_json(self.last_seen, {"brambory": {"lidl": "2026-08-01"}})
        table, rep = self.build([], previous=prev)
        self.assertEqual(table["perKg"], {"maslo": {"lidl": 7.9}})          # brambory: > 21 days -> dropped
        self.assertEqual(table["deals"][0]["perKg"], 7.2)                    # store missing today keeps its deals
        self.assertEqual((rep["staleCount"], rep["droppedCount"], rep["dealsCarried"]), (1, 1, 1))

    def test_cz_writes_v1_and_v2(self):
        items = [{"ingredientId": "maslo", "store": "globus", "czkPerKg": 159.6, "promo": False, "title": "Máslo"}]
        v2 = os.path.join(self.root, "prices", "cz.json")
        bp.dump_json(self.matched, items)
        table, rep = bp.build(self.matched, self.v1, os.path.join(self.root, "last_seen.json"), self.catalog,
                              self.fallback, build_report_path=None, today=dt.date(2026, 9, 19), market="cz",
                              prices_v2_path=v2)
        self.assertEqual(table["v"], 1)
        self.assertEqual(table["czkPerKg"], {"maslo": {"globus": 159.6}})
        self.assertEqual(table["categoryFallbackCzkPerKg"]["dairy"], 120)
        doc = json.load(open(v2, encoding="utf-8"))
        self.assertEqual((doc["v"], doc["market"], doc["currency"], doc["perKg"]), (2, "cz", "CZK", {"maslo": {"globus": 159.6}}))
        self.assertEqual(doc["categoryFallbackPerKg"]["dairy"], 120)


# ---- validator v2 -----------------------------------------------------------------
GOOD_V2 = {
    "v": 2, "market": "sk", "currency": "EUR", "updated": "2026-09-19",
    "perKg": {"maslo": {"lidl": 7.96, "coopJednota": 8.4}},
    "deals": [{"ingredientId": "maslo", "store": "lidl", "perKg": 7.2, "validFrom": "2026-09-19",
               "validTo": "2026-09-25", "title": "Maslo akcia"}],
    "categoryFallbackPerKg": {"dairy": 4.92},
}
V2_TODAY = dt.date(2026, 9, 19)


class ValidateV2Case(unittest.TestCase):
    def setUp(self):
        self.schema = bp.load_json(validate.SCHEMA_V2_PATH)
        self.assertIsNotNone(self.schema)

    def test_good_v2_passes_schema_and_rules(self):
        self.assertEqual(validate.schema_errors(GOOD_V2, self.schema), [])
        self.assertEqual(validate.rule_errors(GOOD_V2, {"maslo"}, V2_TODAY, "sk"), [])
        mini: list[str] = []
        validate._mini_validate(GOOD_V2, self.schema, self.schema, "$", mini)
        self.assertEqual(mini, [])

    def test_v2_schema_rejects_v1_keys_and_unknown_store(self):
        bad = copy.deepcopy(GOOD_V2)
        bad["czkPerKg"] = bad.pop("perKg")
        self.assertTrue(any("perKg" in e for e in validate.schema_errors(bad, self.schema)))
        bad = copy.deepcopy(GOOD_V2)
        bad["perKg"]["maslo"]["kosik"] = 5
        self.assertTrue(validate.schema_errors(bad, self.schema))

    def test_v2_rules_enforce_market_stores_and_currency(self):
        bad = copy.deepcopy(GOOD_V2)
        bad["perKg"]["maslo"]["globus"] = 7.5          # valid store enum, but Czech
        errs = validate.rule_errors(bad, {"maslo"}, V2_TODAY, "sk")
        self.assertTrue(any("globus" in e and "unknown store" in e for e in errs))
        bad = copy.deepcopy(GOOD_V2)
        bad["currency"] = "CZK"
        self.assertTrue(any(e.startswith("currency:") for e in validate.rule_errors(bad, {"maslo"}, V2_TODAY)))
        self.assertTrue(any(e.startswith("market:") for e in validate.rule_errors(GOOD_V2, {"maslo"}, V2_TODAY, "pl")))
        bad = copy.deepcopy(GOOD_V2)
        bad["deals"][0]["validTo"] = "2026-09-01"
        self.assertTrue(any("expired" in e for e in validate.rule_errors(bad, {"maslo"}, V2_TODAY, "sk")))
        v1 = {"v": 1, "updated": "2026-09-19", "czkPerKg": {}, "deals": [], "categoryFallbackCzkPerKg": {}}
        self.assertTrue(any("Czech only" in e for e in validate.rule_errors(v1, set(), V2_TODAY, "sk")))
        self.assertEqual(validate.rule_errors(v1, set(), V2_TODAY, "cz"), [])

    def test_validate_file_picks_schema_by_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "sk.json")
            bp.dump_json(p, GOOD_V2)
            self.assertEqual(validate.validate_file(p, today=V2_TODAY, require_catalog=False, market="sk"), [])
            self.assertEqual(validate.main([p, "--market", "sk", "--today", "2026-09-19", "--no-catalog"]), 0)
            bad = copy.deepcopy(GOOD_V2)
            bad["perKg"]["maslo"]["globus"] = 1
            bp.dump_json(p, bad)
            self.assertEqual(validate.main([p, "--market", "sk", "--today", "2026-09-19", "--no-catalog"]), 1)


# ---- report per market ---------------------------------------------------------------
class ReportMarketCase(unittest.TestCase):
    def test_write_all_for_sk(self):
        with tempfile.TemporaryDirectory() as tmp:
            matched = os.path.join(tmp, "sk", "matched.json")
            bp.dump_json(matched, {"v": 1, "market": "sk", "items": [
                {"ingredientId": "maslo", "store": "lidl", "source": "lidl_sk", "czkPerKg": 7.96, "status": "matched", "title": "Maslo"},
                {"ingredientId": "maslo", "store": "lidl", "source": "lidl_sk", "czkPerKg": 7.96, "status": "review", "title": "Maslo?"},
            ]})
            bp.dump_json(os.path.join(tmp, "sk", "offers.json"),
                         {"offers": [{"title": "Maslo", "store": "lidl", "source": "lidl_sk"}]})
            bp.dump_json(os.path.join(tmp, "sk", "health.json"),
                         {"market": "sk", "sources": {"lidl_sk": {"ok": True, "count": 1, "requests": 2, "market": "sk"}},
                          "notFetched": ["tesco", "kaufland", "billa", "coopJednota", "terno", "fresh"]})
            prices = os.path.join(tmp, "prices", "sk.json")
            bp.dump_json(prices, GOOD_V2)
            panel = os.path.join(tmp, "data", "sk")
            rep = report.write_all(V2_TODAY, matched, prices, os.path.join(tmp, "sk", "report.json"),
                                   os.path.join(tmp, "sk", "history.json"), panel,
                                   os.path.join(tmp, "sk", "build_report.json"), publish_catalog_copy=False,
                                   market="sk", offers_path=os.path.join(tmp, "sk", "offers.json"),
                                   health_path=os.path.join(tmp, "sk", "health.json"), markets_summary_path=None)
            self.assertEqual((rep["market"], rep["currency"], rep["ok"]), ("sk", "EUR", True))
            self.assertEqual(rep["prices"]["v"], 2)
            self.assertEqual(rep["prices"]["perStore"]["coopJednota"], 1)
            self.assertEqual(rep["matching"]["matchedPerStore"], {s: (1 if s == "lidl" else 0) for s in markets.stores_of("sk")})
            self.assertEqual((rep["matching"]["review"], rep["matching"]["unmatched"]), (1, 0))
            self.assertFalse(any("tesco" in w for w in rep["warnings"]))     # not fetched -> no carry-forward warning
            health = bp.load_json(os.path.join(panel, "health.json"))
            self.assertEqual((health["market"], health["currency"]), ("sk", "EUR"))
            self.assertEqual(set(health["perStore"]), set(markets.stores_of("sk")))
            self.assertTrue(health["perStore"]["terno"]["notFetched"] and not health["perStore"]["lidl"]["notFetched"])
            self.assertEqual(bp.load_json(os.path.join(tmp, "sk", "history.json"))[0]["market"], "sk")

    def test_markets_summary_lists_every_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            bp.dump_json(os.path.join(tmp, "health.json"), {"date": "2026-09-19", "ok": True, "status": "ok",
                                                            "run": {"priced": 200, "deals": 700}, "sources": {"globus": {"ok": True}}})
            bp.dump_json(os.path.join(tmp, "sk", "health.json"), {"date": "2026-09-19", "ok": True, "status": "warning",
                                                                  "run": {"priced": 19, "deals": 34},
                                                                  "sources": {"lidl_sk": {"ok": False, "error": "x"}},
                                                                  "notFetched": ["tesco"]})
            doc = report.markets_summary(tmp)
            self.assertEqual(list(doc["markets"]), ["cz", "sk", "pl", "de", "at"])
            self.assertEqual((doc["markets"]["cz"]["priced"], doc["markets"]["cz"]["sourcesOk"], doc["markets"]["cz"]["currency"]), (200, 1, "CZK"))
            self.assertEqual((doc["markets"]["sk"]["sourcesError"], doc["markets"]["sk"]["notFetched"], doc["markets"]["sk"]["prices"]),
                             (1, ["tesco"], "prices/sk.json"))
            self.assertEqual(doc["markets"]["pl"]["status"], "missing")


def test_run_all_passes_market_to_every_per_market_step():
    sys.path.insert(0, REPO)
    import run_all
    ns = run_all.argparse.Namespace(today="2026-09-19")
    assert run_all.step_args("fetch", "sk", ns, ["fetch"], set(), "t") == ["--market", "sk"]
    mapper = run_all.step_args("mapper", "sk", ns, ["mapper"], set(), "t")
    assert mapper[:2] == ["--market", "sk"] and mapper[3].endswith(os.path.join("sk", "offers.json")) and mapper[5].endswith("sk")
    assert run_all.step_args("ratings", "cz", ns, ["ratings"], set(), "t") == ["--today", "2026-09-19"]
    assert run_all.step_args("validate", "at", ns, ["validate"], set(), "t") == ["--market", "at", "--today", "2026-09-19"]
    assert run_all.step_args("report", "pl", ns, ["fetch", "report"], set(), "t")[-2:] == ["--started-at", "t"]

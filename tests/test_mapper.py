"""Unit tests for pipeline/mapper.py.

Run from the repository root:
    python -m unittest discover -s tests -p "test_mapper*"   (or: python -m pytest tests/test_mapper.py)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import mapper as M  # noqa: E402

CATALOG = M.Catalog.load()
RULES = M.load_rules()
MAPPER = M.Mapper(CATALOG, RULES)


def _offer(title: str, **kw) -> dict:
    o = {"title": title, "store": "lidl"}
    o.update(kw)
    return o


def _priced(title: str, expected_id: str | None, **kw) -> dict:
    """An offer priced at the ingredient's reference price so the price sanity
    check does not interfere with the mapping assertions."""
    ref = CATALOG.items[expected_id].ref_price if expected_id else 100
    o = _offer(title, price=ref, pack="1 kg")
    o.update(kw)
    return o


class TextNormalizerParity(unittest.TestCase):
    """Mirror of lib/logic/text_normalizer.dart."""

    def test_fold(self):
        self.assertEqual(M.fold("Kuřecí prsa – chlazená"), "kureci prsa chlazena")
        self.assertEqual(M.fold("  Müller  Milch "), "muller milch")
        self.assertEqual(M.fold("Ærø ß œ"), "aro ss oe")
        self.assertEqual(M.fold("Łódź Győr Ştefan"), "lodz gyor stefan")
        self.assertEqual(M.fold("Phở bò"), "pho bo")
        self.assertEqual(M.fold("😀 x"), "x")
        self.assertEqual(M.fold("abc"), "abc")
        self.assertEqual(M.fold(""), "")
        self.assertEqual(M.fold("1,5 %"), "1 5")

    def test_tokens(self):
        self.assertEqual(M.tokens("a bc d ef"), ["bc", "ef"])
        self.assertEqual(M.tokens(""), [])

    def test_stem(self):
        cases = {
            "smetana": "smetan", "kureci": "kurec", "prsa": "prsa", "jogurt": "jogurt",
            "mlekem": "mlek", "vejce": "vejc", "rajcata": "rajc", "brambory": "brambor",
            "kroupy": "kroup", "salat": "salat", "salatu": "salat", "maso": "maso",
            "stehna": "stehn", "maslo": "masl", "olej": "olej", "cibule": "cibul",
            "syr": "syr", "mlete": "mlet", "hovezi": "hovez", "krkovice": "krkovic",
        }
        for word, expected in cases.items():
            self.assertEqual(M.stem(word), expected, word)

    def test_normalize_tokens(self):
        self.assertEqual(M.normalize_tokens("Kuřecí prsní řízky"), ["kurec", "prsn", "rizk"])

    def test_token_matches(self):
        self.assertTrue(M.token_matches("kurec", "kureci"))
        self.assertTrue(M.token_matches("smetanov", "smetan"))
        self.assertTrue(M.token_matches("ab", "ab"))
        self.assertFalse(M.token_matches("ab", "abc"))
        self.assertFalse(M.token_matches("prsa", "prsn"))
        self.assertFalse(M.token_matches("", "abc"))


class PackParsing(unittest.TestCase):
    def test_parse_pack(self):
        def p(text):
            r = M.parse_pack(text)
            return (round(r.amount, 4), r.unit) if r else None
        self.assertEqual(p("250 g"), (0.25, "kg"))
        self.assertEqual(p("500g"), (0.5, "kg"))
        self.assertEqual(p("1kg"), (1.0, "kg"))
        self.assertEqual(p("2,5 kg"), (2.5, "kg"))
        self.assertEqual(p("0,5 l"), (0.5, "l"))
        self.assertEqual(p("1,5 l"), (1.5, "l"))
        self.assertEqual(p("330 ml"), (0.33, "l"))
        self.assertEqual(p("10 ks"), (10.0, "ks"))
        self.assertEqual(p("30Ks"), (30.0, "ks"))
        self.assertEqual(p("2x100 g"), (0.2, "kg"))
        self.assertEqual(p("6x65g"), (0.39, "kg"))
        self.assertEqual(p("44 x 85 g"), (3.74, "kg"))
        self.assertEqual(p("cca.140g"), (0.14, "kg"))
        self.assertEqual(p("Vejce M 10 ks"), (10.0, "ks"))
        self.assertIsNone(p("Dostupné pouze ve vybraných prodejnách"))
        self.assertIsNone(p(None))

    def test_parse_unit_price(self):
        def u(text):
            r = M.parse_unit_price(text)
            return (round(r[0], 2), r[1]) if r else None
        self.assertEqual(u("15,96 Kč / 100 g"), (159.6, "kg"))
        self.assertEqual(u("10,80 Kč / 1 kg"), (10.8, "kg"))
        self.assertEqual(u("8,90 Kč / 1 l"), (8.9, "l"))
        self.assertEqual(u("250 g, 100 g = 15,96 Kč"), (159.6, "kg"))
        self.assertEqual(u("1 kg = 43 Kč"), (43.0, "kg"))
        self.assertEqual(u("30 ks, 1 kus = 2,50 Kč"), (2.5, "ks"))
        self.assertEqual(u("400 ml; 1 l = 247,25 Kč"), (247.25, "l"))
        self.assertEqual(u("100 g = od 39,95 Kč"), (399.5, "kg"))
        self.assertIsNone(u("cena za 100 g"))
        self.assertIsNone(u("1 kg"))

    def test_price_basis(self):
        b = M.parse_price_basis("cena za 100 g")
        self.assertEqual((b.amount, b.unit), (0.1, "kg"))


class PricePerKg(unittest.TestCase):
    def kg(self, offer):
        r = MAPPER.map_offer(offer)
        return r["czkPerKg"], r["priceMethod"], r

    def test_eggs_per_piece_use_55g(self):
        kg, method, r = self.kg(_offer("Vejce M 10 ks", price=49.9, pack="10 ks"))
        self.assertEqual(r["ingredientId"], "vejce")
        self.assertAlmostEqual(kg, 4.99 / 0.055, places=1)
        self.assertEqual(method, "pack")

    def test_butter_pack_in_title(self):
        kg, _, r = self.kg(_offer("Máslo 250 g", price=39.9))
        self.assertEqual(r["ingredientId"], "maslo")
        self.assertAlmostEqual(kg, 159.6, places=2)

    def test_lidl_unit_price_text(self):
        kg, method, _ = self.kg(_offer("Laktos České máslo", price=39.9, unitPriceText="250 g, 100 g = 15,96 Kč"))
        self.assertAlmostEqual(kg, 159.6, places=2)
        self.assertEqual(method, "offer.unitPriceText")

    def test_lidl_price_per_100g(self):
        kg, _, r = self.kg(_offer("Smetanový sýr", price=56.9, unitPriceText="cena za 100 g"))
        self.assertEqual(r["ingredientId"], "smetanovy_syr")
        self.assertAlmostEqual(kg, 569.0, places=2)

    def test_lidl_plain_kg_basis(self):
        kg, _, r = self.kg(_offer("Cibule žlutá", price=9.9, unitPriceText="1 kg"))
        self.assertEqual(r["ingredientId"], "cibule")
        self.assertAlmostEqual(kg, 9.9, places=2)
        kg, _, r = self.kg(_offer("Ledový salát", price=19.9, unitPriceText="kus"))
        self.assertEqual(r["ingredientId"], "salat_ledovy")
        self.assertAlmostEqual(kg, 19.9 / 0.5, places=2)

    def test_globus_structured_unit_price(self):
        kg, method, r = self.kg(_offer("Kuřecí prsní řízky chlazené", store="globus", price=99.9,
                                       unitPrice={"czk": 139.8, "per": "1 kg"}))
        self.assertEqual(r["ingredientId"], "kureci_prsa")
        self.assertEqual((kg, method), (139.8, "offer.unitPrice"))

    def test_penny_per_100g_structured(self):
        kg, _, r = self.kg(_offer("Tvaroh tučný Karlova Koruna", store="penny", price=12.9,
                                  unitPrice={"czk": 5.16, "per": "100 g"}))
        self.assertEqual(r["ingredientId"], "tvaroh")
        self.assertAlmostEqual(kg, 51.6, places=2)

    def test_liquid_uses_density(self):
        kg, _, r = self.kg(_offer("Pilsner Urquell Pivo ležák 0,5 l", store="globus", price=25.9))
        self.assertEqual(r["ingredientId"], "pivo_lezak")
        self.assertAlmostEqual(kg, 51.8 / 1.01, places=1)
        kg, _, r = self.kg(_offer("Trvanlivé mléko 1,5%", price=12.9, pack="1 l"))
        self.assertEqual(r["ingredientId"], "mleko_15")
        self.assertAlmostEqual(kg, 12.9 / 1.03, places=2)

    def test_multipack(self):
        kg, _, _ = self.kg(_offer("Calvo Tuňák v olivovém oleji 6x65g", store="globus", price=169.9))
        self.assertAlmostEqual(kg, 169.9 / 0.39, places=1)

    def test_fetcher_per_kg_is_fallback_only(self):
        kg, method, _ = self.kg(_offer("Máslo 250 g", price=39.9, czkPerKg=150.0))
        self.assertEqual((kg, method), (159.6, "pack"))
        kg, method, _ = self.kg(_offer("Máslo", price=39.9, price_per_kg=150.0))
        self.assertEqual((kg, method), (150.0, "offer.price_per_kg"))

    def test_fetch_layer_offer_shape(self):
        """providers.base.Offer.to_dict() - snake_case plus D1's camelCase aliases."""
        offer = {"store": "globus", "title": "Pilsner Urquell Pivo ležák světlý sklo 0,5l", "price_czk": 25.9,
                 "unit": "l", "quantity": 0.5, "price_per_kg": 51.8, "valid_from": "2026-09-09",
                 "valid_to": "2026-09-15", "source_url": "https://www.globus.cz/x", "source": "globus",
                 "original_price_czk": None, "club": False, "is_promo": False, "ingredient_id": None,
                 "raw": {"categories": ["cls_czr_lagers_up_to_12", "cls_czr_beer"], "vanr": "1"}}
        r = MAPPER.map_offer(offer)
        self.assertEqual((r["status"], r["ingredientId"], r["store"]), ("matched", "pivo_lezak", "globus"))
        self.assertAlmostEqual(r["czkPerKg"], 51.8 / 1.01, places=1)
        self.assertEqual((r["price"], r["validFrom"], r["validTo"], r["url"], r["promo"], r["club"]),
                         (25.9, "2026-09-09", "2026-09-15", "https://www.globus.cz/x", False, False))
        self.assertNotIn("raw", r)
        # kupi term prior: accepted when the title agrees, flagged when it does not
        r = MAPPER.map_offer({"store": "kaufland", "title": "Máslo Jihočeské Madeta", "price_czk": 39.9, "unit": "g",
                              "quantity": 250, "source": "kupi", "ingredient_id": "maslo", "is_promo": True})
        self.assertEqual((r["status"], r["ingredientId"], r["method"], r["promo"]), ("matched", "maslo", "term+score", True))
        r = MAPPER.map_offer({"store": "kaufland", "title": "Selské máslo 84%", "price_czk": 49.9, "unit": "g",
                              "quantity": 250, "source": "kupi", "ingredient_id": "maslo"})
        self.assertEqual((r["status"], r["ingredientId"]), ("matched", "maslo"))
        r = MAPPER.map_offer({"store": "kaufland", "title": "Dýně máslová", "price_czk": 24.9, "unit": "kg",
                              "quantity": 1, "source": "kupi", "ingredient_id": "maslo"})
        self.assertNotEqual(r["ingredientId"], "maslo")
        self.assertIn("term-mismatch:maslo", r["reasons"])
        # a clearly better id beats the term prior
        r = MAPPER.map_offer({"store": "albert", "title": "Hořčice dijonská Maille", "price_czk": 49.9, "unit": "ml",
                              "quantity": 200, "source": "kupi", "ingredient_id": "horcice"})
        self.assertEqual(r["ingredientId"], "horcice_dijonska")

    def test_liquid_pack_on_solid_goes_to_review(self):
        r = MAPPER.map_offer(_offer("Moravská Švestka", price=139.9, pack="0,5 l"))
        self.assertEqual(r["status"], "review")
        self.assertIn("liquid-pack-for-solid", r["flags"])

    def test_string_price(self):
        kg, _, r = self.kg(_offer("Máslo 250 g", price="39,90 Kč"))
        self.assertAlmostEqual(kg, 159.6, places=2)
        self.assertEqual(r["price"], 39.9)

    def test_no_pack_goes_to_review(self):
        r = MAPPER.map_offer(_offer("Kuřecí stehna", price=89.9))
        self.assertEqual(r["status"], "review")
        self.assertIn("no-unit-price", r["flags"])
        self.assertEqual(r["ingredientId"], "kureci_stehna")

    def test_suspicious_price_goes_to_review(self):
        r = MAPPER.map_offer(_offer("Máslo", price=5.0, pack="1 kg"))
        self.assertEqual(r["status"], "review")
        self.assertTrue(any(f.startswith("price-suspicious") for f in r["flags"]))


class TitleMatching(unittest.TestCase):
    """Real leaflet / API titles (Globus, Lidl, Penny, kupi) -> catalog ids."""

    AUTO = [
        # Globus action API
        ("Kuřecí prsní řízky chlazené", "kureci_prsa"),
        ("Rajčata Cherry červená Hranáček 500g", "rajcata_cherry"),
        ("Švestky volné", "svestky"),
        ("Sýr Eidam 45% plátky", "eidam"),
        ("Vepřové mleté Globus", "mlete_veprove"),
        ("Vepřová kýta Globus", "veprova_kyta"),
        ("Hovězí žebro s kostí Globus", "hovezi_zebra"),
        ("Barilla Spaghetti 500 g", "spagety"),
        ("Brölio Jedlý slunečnicový olej 1 l", "olej_slunecnicovy"),
        ("Rabbit Štěpánovské kuřecí mleté maso 500g", "mlete_kureci"),
        ("Citróny balené 500 g", "citron"),
        ("Calvo Tuňák v olivovém oleji 6x65g", "tunak_konzerva"),
        ("Mutti Jemně krájená rajčata 400 g", "rajcata_sterilovana"),
        ("Bonduelle Bon Menu Červená fazole Barbecue 430g", "fazole_cervene_sterilovane"),
        ("Pilsner Urquell Pivo ležák světlý sklo 0,5l", "pivo_lezak"),
        ("Milka Lískooříšková pomazánka 600g", "pomazanka_liskoorechova"),
        ("Hovězí plátky - kýta, 2 kusy Globus  - hovězí zadní maso", "hovezi_zadni"),
        # Lidl category API
        ("Laktos České máslo", "maslo"),
        ("Trvanlivé mléko 1,5%", "mleko_15"),
        ("Trvanlivé mléko 3,5%", "mleko_35"),
        ("PILOS TRADIČNÍ Bílý jogurt", "jogurt_bily"),
        ("Podestýlková vejce „M”", "vejce"),
        ("Česká vejce Vejce L 10ks podestýlka", "vejce"),
        ("BOHEMILK Tradiční smetana ke šlehání", "smetana_33"),
        ("Madeta Jihočeský Eidam", "eidam"),
        ("PILOS TRADIČNÍ Kefírové mléko", "kefir"),
        ("JAROMĚŘICKÁ MLÉKÁRNA Jaroměřické žervé", "smetanovy_syr"),
        ("Cibule žlutá", "cibule"),
        ("Zelí bílé", "zeli_bile"),
        ("Ledový salát", "salat_ledovy"),
        ("Batáty", "batat"),
        ("Hrušky", "hruska"),
        ("Pomeranče", "pomeranc"),
        ("Nektarinky", "broskve"),
        ("BIO Kuřecí prsní řízky", "kureci_prsa"),
        ("Kuřecí stehenní řízky", "kureci_stehenni_rizky"),
        ("Kachní prsa", "kachni_prsa"),
        ("ČESTR Hovězí zadní kýta", "hovezi_zadni"),
        ("Pšeničná mouka hladká", "mouka_hladka"),
        ("Rostlinný tuk na pečení", "margarin"),
        ("MAGNESIA Minerální voda", "mineralka"),
        ("Dlouhozrnná rýže v varných sáčcích", "ryze_dlouhozrnna"),
        ("Bio Čerstvé mléko 4 %", "mleko_35"),
        # Penny web API
        ("Tvaroh tučný Karlova Koruna", "tvaroh"),
        # kupi.cz / leaflet style
        ("Máslo Jihočeské Madeta", "maslo"),
        ("Dýně máslová", "dyne_hokkaido"),
        ("Arašídové máslo", "arasidove_maslo"),
        ("Kuřecí stehenní řízky bez kosti", "kureci_stehenni_rizky"),
        ("Vepřová krkovice bez kosti", "veprova_krkovice"),
        ("Vepřová kotleta s kostí", "veprova_kotleta"),
        ("Hovězí zadní bez kosti", "hovezi_zadni"),
        ("Krůtí prsa", "kruti_prsa"),
        ("Vepřový bůček", "veprovy_bucek"),
        ("Vepřová panenka", "veprova_panenka"),
        ("Losos filet", "losos"),
        ("Vejce z podestýlky M 10 ks", "vejce"),
        ("Vajíčka 10 ks", "vejce"),
        ("Cukr krystal 1 kg", "cukr"),
        ("Hladká mouka 1 kg", "mouka_hladka"),
        ("Rýže dlouhozrnná 1 kg", "ryze_dlouhozrnna"),
        ("Rýže natural", "ryze_natural"),
        ("Olivový olej extra panenský 750 ml", "olej_olivovy"),
        ("Kečup jemný 500 g", "kecup"),
        ("Hořčice plnotučná 350 g", "horcice"),
        ("Banány", "banan"),
        ("Brambory konzumní pozdní 5 kg", "brambory"),
        ("Mrkev 1 kg", "mrkev"),
        ("Paprika červená", "paprika_cervena"),
        ("Okurka salátová", "okurka_salatova"),
        ("Špagety 500 g", "spagety"),
        ("Zakysaná smetana 15%", "smetana_zakysana"),
        ("Smetana ke šlehání 31 %", "smetana_33"),
        ("Smetana na vaření 12 %", "smetana_12"),
        ("Mléko 1 l", "mleko_15"),
        ("Mléko polotučné trvanlivé 1 l", "mleko_15"),
        ("Kuřecí křídla", "kureci_kridla"),
        ("Kuřecí stehna", "kureci_stehna"),
        ("Kuřecí čtvrtky", "kureci_stehna"),
        ("Kuře celé", "kure_cele"),
        ("Kuře bez drobů", "kure_cele"),
        ("Mleté maso mix", "mlete_maso_mix"),
        ("Šunka nejvyšší jakosti", "sunka"),
        ("Gothajský salám", "salam_gothaj"),
        ("Tvaroh měkký 250 g", "tvaroh"),
        ("Balkánský sýr", "balkansky_syr"),
        ("Ementál", "emental"),
        ("Mozzarella", "mozzarella"),
        ("Sýr Gouda 48% plátky", "gouda"),
        ("Hermelín", "hermelin"),
        ("Jahody 250 g", "jahody"),
        ("Žampiony 250 g", "zampiony"),
        ("Zmrzlina smetanová jahoda 420 ml", "zmrzlina_vanilkova"),
    ]

    # Plausible but not certain: an unknown adjective next to a generic noun.
    REVIEW = [
        ("Chléb Horal Globus 500 g", "chleb"),
        ("Selské máslo 84%", "maslo"),
        ("Jablka Golden", "jablko"),
        ("Kuřecí šunka", "sunka"),
        ("Bezlaktózové trvanlivé mléko 1,5%", "mleko_15"),
        ("Chilli sýr", "eidam"),
        ("Gouda se zeleným pestem cca.140g", None),
        ("Filé z aljašské tresky", None),
    ]

    # Must never be auto-accepted.
    NOT_AUTO = [
        "Pomazánkové máslo",
        "Leerdammer Original 100 g",
        "Znojmia Moravanka sterilovaná pikantní směs 330 g",
        "Jelení kýta bez kosti mražená 800g",
        "Lotus Biscoff Sandwich Milk Chocolate 150g",
        "Junior salám Globus",
        "Šála dámská",
        "Blue seven kabát pánský",
        "Perla Margarín s máslovou příchutí 39% tuku",
    ]

    def test_auto_accepted(self):
        failures = []
        for title, expected in self.AUTO:
            r = MAPPER.map_offer(_priced(title, expected))
            if r["status"] != "matched" or r["ingredientId"] != expected:
                failures.append((title, expected, r["status"], r["ingredientId"], r["confidence"],
                                 [(s["ingredientId"], s["score"]) for s in r["suggestions"][:3]]))
        self.assertEqual(failures, [], "\n" + "\n".join(map(str, failures)))

    def test_review_tier(self):
        for title, expected in self.REVIEW:
            r = MAPPER.map_offer(_priced(title, expected))
            self.assertEqual(r["status"], "review", (title, r["status"], r["confidence"]))
            self.assertTrue(M.REVIEW_MIN <= r["confidence"] < M.AUTO_ACCEPT, (title, r["confidence"]))
            if expected:
                self.assertEqual(r["suggestions"][0]["ingredientId"], expected, title)
            self.assertLessEqual(len(r["suggestions"]), 3)

    def test_never_auto(self):
        for title in self.NOT_AUTO:
            r = MAPPER.map_offer(_offer(title, price=100, pack="1 kg"))
            self.assertNotEqual(r["status"], "matched", (title, r["ingredientId"], r["confidence"]))

    def test_negative_tokens(self):
        r = MAPPER.map_offer(_offer("Arašídové máslo 350 g", price=79.9))
        self.assertNotEqual(r["ingredientId"], "maslo")
        r = MAPPER.map_offer(_offer("Kokosové mléko 400 ml", price=39.9))
        self.assertEqual(r["ingredientId"], "kokosove_mleko")

    def test_fat_percent_splits_siblings(self):
        r = MAPPER.map_offer(_priced("Čerstvé mléko 3,5%", "mleko_35", pack="1 l"))
        self.assertEqual(r["ingredientId"], "mleko_35")
        self.assertEqual(r["suggestions"][1]["ingredientId"], "mleko_15")
        r = MAPPER.map_offer(_priced("Čerstvé mléko 1,5%", "mleko_15", pack="1 l"))
        self.assertEqual(r["ingredientId"], "mleko_15")

    def test_category_sanity_from_title(self):
        # "smetana" must land on a dairy id; fruit "jahody" is penalised
        ranked = M.score_title(M.analyze_title("Smetanová zmrzlina jahoda"), CATALOG)
        self.assertEqual(CATALOG.items[ranked[0].ingredient_id].category, "dairy")
        for s in ranked:
            if CATALOG.items[s.ingredient_id].category == "fruit":
                self.assertLess(s.score, M.REVIEW_MIN, (s.ingredient_id, s.score))
                self.assertTrue(any(r.startswith("title-category") for r in s.reasons), s.reasons)
        # "kuřecí prsa" must be poultry
        r = MAPPER.map_offer(_priced("Kuřecí prsa 1 kg", "kureci_prsa"))
        self.assertEqual(CATALOG.items[r["ingredientId"]].category, "poultry")

    def test_category_hint_union(self):
        self.assertEqual(M.category_from_hint(["cls_czr_fruits_and_vegetables", "cls_czr_fruit"]) & {"fruit"}, {"fruit"})
        self.assertEqual(M.category_from_hint("Potřeby pro domácí mazlíčky/Krmivo pro psy"), M.NONFOOD)
        self.assertIsNone(M.category_from_hint([]))
        r = MAPPER.map_offer(_offer("Felix Fantastic hovězí v želé 44 x 85g", store="globus", price=269.9,
                                    category=["cls_czr_pockets_and_pates_for_cats"]))
        self.assertEqual(r["status"], "ignored")

    def test_store_normalisation(self):
        self.assertEqual(M.normalize_store("Penny Market"), "penny")
        self.assertEqual(M.normalize_store("Albert hypermarket"), "albert")
        self.assertEqual(M.normalize_store("GLOBUS"), "globus")
        self.assertIsNone(M.normalize_store("Košík"))
        r = MAPPER.map_offer({"title": "Máslo 250 g", "shop": "Penny Market", "price": 39.9})
        self.assertEqual(r["store"], "penny")
        r = MAPPER.map_offer({"title": "Máslo 250 g", "store": "Košík", "price": 39.9})
        self.assertNotEqual(r["status"], "matched")
        self.assertIn("unknown-store", r["reasons"])


class Rules(unittest.TestCase):
    def test_bundled_ignore_rules(self):
        r = MAPPER.map_offer(_offer("Konzerva pro psy s hovězím", price=27.9, pack="1250 g"))
        self.assertEqual(r["status"], "ignored")
        self.assertEqual(r["method"], "rule")

    def test_rule_forms_and_store_filter(self):
        rules = [
            M.Rule("junior salam", "globus", "salam_gothaj", "test"),
            M.Rule("sub:chleban", "*", "chleb", "test"),
            M.Rule("re:^leerdammer\\b", "*", "emental", "test"),
        ]
        mp = M.Mapper(CATALOG, rules)
        r = mp.map_offer(_offer("Junior salám Globus", store="globus", price=169, pack="1 kg"))
        self.assertEqual((r["status"], r["ingredientId"], r["method"]), ("matched", "salam_gothaj", "rule"))
        r = mp.map_offer(_offer("Junior salám Globus", store="lidl", price=169, pack="1 kg"))
        self.assertNotEqual(r["method"], "rule")
        r = mp.map_offer(_offer("Chlebánek krájený, balený Globus 250g", store="globus", price=12.9))
        self.assertEqual(r["ingredientId"], "chleb")
        r = mp.map_offer(_offer("Leerdammer Original 100 g", price=29.9))
        self.assertEqual(r["ingredientId"], "emental")
        self.assertAlmostEqual(r["czkPerKg"], 299.0, places=2)

    def test_rule_price_still_checked(self):
        mp = M.Mapper(CATALOG, [M.Rule("xyz", "*", "maslo", "")])
        r = mp.map_offer(_offer("xyz", price=5, pack="1 kg"))
        self.assertEqual(r["status"], "review")


class Outputs(unittest.TestCase):
    def test_write_outputs_round_trip(self):
        offers = [
            _offer("Máslo 250 g", price=39.9, url="https://example.test/a", validFrom="2026-09-16", validTo="2026-09-22"),
            _offer("Chilli sýr", price=56.9, unitPriceText="cena za 100 g"),
            _offer("Znojmia Moravanka sterilovaná pikantní směs 330 g", price=29.9),
            _offer("Konzerva pro psy", price=11.9),
        ]
        matched, rest = MAPPER.map_offers(offers)
        with tempfile.TemporaryDirectory() as tmp:
            mp, up = M.write_outputs(matched, rest, tmp, today="2026-09-15")
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            with open(up, encoding="utf-8") as f:
                u = json.load(f)
        self.assertEqual(m["counts"], {"matched": 1, "review": 1, "unmatched": 1, "ignored": 1})
        self.assertEqual(m["items"][0]["ingredientId"], "maslo")
        self.assertEqual(m["items"][0]["validTo"], "2026-09-22")
        self.assertEqual(m["items"][0]["url"], "https://example.test/a")
        statuses = [i["status"] for i in u["items"]]
        self.assertEqual(statuses, ["review", "unmatched", "ignored"])
        review = u["items"][0]
        for key in ("title", "store", "price", "suggestions", "confidence"):
            self.assertIn(key, review)
        self.assertEqual(review["suggestions"][0]["ingredientId"], "eidam")

    def test_load_offers_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "o.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"offers": [{"title": "x"}]}, f)
            self.assertEqual(M.load_offers(p), [{"title": "x"}])
            with open(p, "w", encoding="utf-8") as f:
                json.dump([{"title": "y"}], f)
            self.assertEqual(M.load_offers(p), [{"title": "y"}])


class CatalogSync(unittest.TestCase):
    def test_catalog_copy_matches_app_shape(self):
        with open(M.CATALOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["v"], 1)
        self.assertGreaterEqual(len(data["ingredients"]), 500)
        first = data["ingredients"][0]
        for key in ("id", "nameCs", "namePluralCs", "aliases", "category", "unitGrams", "densityGPerMl", "refPriceCzkPerKg"):
            self.assertIn(key, first)
        self.assertEqual(CATALOG.items["vejce"].ks_grams, 55.0)

    def test_sync_catalog_rejects_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "bad.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump({"foo": 1}, f)
            with self.assertRaises((ValueError, KeyError, TypeError)):
                M.sync_catalog(src, os.path.join(tmp, "out.json"))


if __name__ == "__main__":
    unittest.main()

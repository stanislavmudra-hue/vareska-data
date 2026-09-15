"""Unit tests for pipeline.providers (fixtures under tests/fixtures, no network)."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import TODAY, FakeHttp, fixture_text
from pipeline import textnorm
from pipeline.providers import albert, base, billa, globus, kaufland, kupi, lidl, penny, publitas, registry
from pipeline.providers.http import RobotsRules


# ---- textnorm mirror -------------------------------------------------------
def test_fold_matches_dart_rules():
    assert textnorm.fold('Kuřecí prsní řízky') == 'kureci prsni rizky'
    assert textnorm.fold('  Máslo - 250g  ') == 'maslo 250g'
    assert textnorm.fold('Œuf ß') == 'oeuf ss'


def test_stem_never_below_four_chars():
    assert textnorm.stem('maslo') == 'masl'
    assert textnorm.stem('prsa') == 'prsa'
    assert textnorm.stem('rajcata') == 'rajc'
    assert textnorm.stem('brambory') == 'brambor'
    assert textnorm.normalize_tokens('Kuřecí prsa s kostí') == ['kurec', 'prsa', 'kost']


def test_token_matches_prefix_rule():
    assert textnorm.token_matches('masl', 'maslov')
    assert not textnorm.token_matches('ab', 'abc')
    assert textnorm.token_matches('ab', 'ab')


# ---- base helpers ----------------------------------------------------------
@pytest.mark.parametrize('text,expected', [
    ('250 g', (250.0, 'g')), ('0,5 l', (0.5, 'l')), ('4 x 125 g', (500.0, 'g')), ('10 ks', (10.0, 'ks')),
    ('1,00 kg', (1.0, 'kg')), ('500ml', (500.0, 'ml')), ('', (None, None)), ('ks', (None, None)),
])
def test_parse_pack(text, expected):
    assert base.parse_pack(text) == expected


def test_parse_unit_price_variants():
    assert base.parse_unit_price('15,96 Kč / 100 g') == pytest.approx(159.6)
    assert base.parse_unit_price('1 kg = 174,75 Kč') == pytest.approx(174.75)
    assert base.parse_unit_price('8,90 Kč / 1 l') == pytest.approx(8.9)
    assert base.parse_unit_price('3,49 Kč / 1 ks', 'Vejce M') == pytest.approx(3.49 / 0.055)
    assert base.parse_unit_price('3,49 Kč / 1 ks', 'Rohlík') is None


def test_offer_price_per_kg_and_eggs():
    o = base.Offer(store='lidl', title='Podestýlková vejce M', price_czk=74.9, unit='ks', quantity=30)
    assert o.price_per_kg == pytest.approx(74.9 / (30 * 0.055), abs=0.01)
    o = base.Offer(store='lidl', title='Máslo', price_czk=39.9, unit='g', quantity=250)
    assert o.price_per_kg == pytest.approx(159.6)
    o = base.Offer(store='lidl', title='Kokosové mléko', price_czk=39.9, unit='ml', quantity=400)
    assert o.price_per_kg == pytest.approx(99.75)
    o = base.Offer(store='lidl', title='Rohlík', price_czk=2.9, unit='ks', quantity=1)
    assert o.price_per_kg is None
    with pytest.raises(ValueError):
        base.Offer(store='kosik', title='x', price_czk=1)


def test_offer_to_dict_has_mapper_aliases():
    o = base.Offer(store='globus', title='Máslo', price_czk=39.9, unit='g', quantity=250, valid_from='2026-09-16',
                   valid_to='2026-09-22', source_url='u', original_price_czk=49.9, ingredient_id='maslo',
                   raw={'categories': ['cls_czr_butter']})
    d = o.to_dict()
    assert (d['price'], d['oldPrice'], d['pack'], d['packAmount'], d['packUnit']) == (39.9, 49.9, '250 g', 250, 'g')
    assert (d['czkPerKg'], d['validFrom'], d['validTo'], d['url'], d['ingredientId']) == (
        159.6, '2026-09-16', '2026-09-22', 'u', 'maslo')
    assert d['category'] == ['cls_czr_butter'] and d['price_czk'] == 39.9


def test_prague_date_from_epoch():
    assert base.prague_date_from_epoch(1789336800) == '2026-09-14'
    assert base.prague_date_from_epoch(1789595999) == '2026-09-16'


def test_nearest_year_rolls_over():
    assert base.nearest_year(5, 1, dt.date(2026, 12, 28)) == '2027-01-05'
    assert base.nearest_year(28, 12, dt.date(2027, 1, 3)) == '2026-12-28'


# ---- robots ----------------------------------------------------------------
def test_robots_wildcards_and_groups():
    r = RobotsRules('User-agent: *\nDisallow: *pageId=*\nDisallow: /hledej?*page=\nUser-agent: AI\nDisallow: /\n')
    assert r.allowed('https://x.cz/q/api/category/h/x/h1?assortment=CZ')
    assert not r.allowed('https://x.cz/q/api/category/h/x/h1?assortment=CZ&pageId=1')
    assert r.allowed('https://x.cz/hledej?f=m%C3%A1slo')
    assert not r.allowed('https://x.cz/hledej?f=x&page=2')
    blocked = RobotsRules('User-agent: Vareska-deals\nDisallow: /\nUser-agent: *\nAllow: /\n')
    assert not blocked.allowed('https://x.cz/anything')


# ---- globus ----------------------------------------------------------------
def test_globus_parse_products():
    http = FakeHttp([(lambda u: 'actionProductsCatalog' in u, 'globus_page0.json')])
    offers = globus.GlobusProvider(http=http, today=TODAY).fetch()
    by_title = {o.title: o for o in offers if not o.club}
    kure = by_title['Kuřecí prsní řízky chlazené']
    assert (kure.unit, kure.quantity, kure.price_czk, kure.price_per_kg) == ('kg', 1.0, 99.9, 99.9)
    assert (kure.valid_from, kure.valid_to, kure.original_price_czk, kure.is_promo) == (
        '2026-09-08', '2026-09-15', 222.9, True)
    pilsner = by_title['Pilsner Urquell Pivo ležák světlý sklo 0,5l']
    assert (pilsner.unit, pilsner.quantity, pilsner.price_per_kg, pilsner.is_promo) == ('l', 0.5, 51.8, False)
    zebro = by_title['Hovězí žebro s kostí Globus']
    assert zebro.valid_to is None  # 9999-12-31 = permanent price
    clubs = [o for o in offers if o.club]
    assert clubs and clubs[0].price_czk < by_title[clubs[0].title].price_czk
    assert http.requests_made == 1


# ---- lidl ------------------------------------------------------------------
def test_lidl_pack_text():
    assert lidl.parse_pack_text('cena za 100 g') == (100.0, 'g')
    assert lidl.parse_pack_text('400 g, 1 kg = 174,75 Kč') == (400.0, 'g')
    assert lidl.parse_pack_text('cena za 1 kg') == (1.0, 'kg')
    assert lidl.parse_pack_text('Dostupné pouze ve vybraných prodejnách') == (None, None)


def test_lidl_walks_categories_and_parses_items():
    http = FakeHttp([
        (lambda u: 'potraviny-a-napoje' in u, 'lidl_root.json'),
        (lambda u: 'maso-a-drubez' in u, 'lidl_maso.json'),
        (lambda u: 'syry-mlecne' in u, 'lidl_vejce.json'),
    ])
    p = lidl.LidlProvider(http=http, today=TODAY)
    offers = p.fetch()
    assert all('pageId' not in u for u in http.urls)  # robots: *pageId=* disallowed
    assert any('domacnost' in u for u in http.urls) is False  # non-food skipped
    titles = {o.title for o in offers}
    assert 'Kuřecí stehenní řízky' in titles and 'Krůtí medailonky' not in titles  # no price -> dropped
    future = next(o for o in offers if o.title == 'Kuřecí stehenní řízky' and o.valid_from)
    assert (future.valid_from, future.valid_to, future.price_per_kg, future.is_promo) == (
        '2026-09-17', '2026-09-20', 89.9, True)
    nudl = next(o for o in offers if o.title.startswith('MASO Z FARMY Vepřové nudličky'))
    assert (nudl.unit, nudl.quantity, nudl.price_per_kg) == ('g', 400.0, 174.75)
    mlete = next(o for o in offers if o.title == 'Vepřové mleté maso')
    assert (mlete.original_price_czk, mlete.valid_to, mlete.price_per_kg) == (74.5, None, 139.8)
    eggs = next(o for o in offers if 'vejce' in o.title.lower())
    assert eggs.unit == 'ks' and eggs.price_per_kg == pytest.approx(eggs.price_czk / (eggs.quantity * 0.055), abs=0.1)


# ---- penny -----------------------------------------------------------------
def test_penny_halere_and_loyalty():
    http = FakeHttp([(lambda u: 'product-discovery' in u, 'penny_page0.json')])
    offers = penny.PennyProvider(http=http, today=TODAY).fetch()
    dup = [o for o in offers if o.title == 'Dupetky']
    assert dup[0].price_czk == 23.9 and dup[0].price_per_kg == 298.8 and not dup[0].club
    assert dup[1].price_czk == 16.9 and dup[1].club and dup[1].original_price_czk == 23.9
    assert (dup[0].valid_from, dup[0].valid_to, dup[0].unit, dup[0].quantity) == ('2026-09-09', '2026-09-15', 'g', 80.0)
    herm = next(o for o in offers if o.title.startswith('Sýr Hermelín'))
    assert (herm.price_czk, herm.original_price_czk, herm.price_per_kg) == (29.9, 56.9, 249.2)
    pyre = next(o for o in offers if o.title.startswith('Rajčatové pyré'))
    assert pyre.price_per_kg == 38.43  # baseUnit kg, factor 1


# ---- publitas / albert / billa ----------------------------------------------
def test_publitas_albert_page():
    spreads = json.loads(fixture_text('albert_spreads.json'))
    offers = publitas.parse_spreads(spreads, 'albert', '2026-09-16', '2026-09-22', 'u', 'albert-text')
    by = {(o.title, o.club): o for o in offers}
    eggs = by[('Vejce z podestýlky M', False)]
    assert (eggs.unit, eggs.quantity) == ('ks', 10.0)
    assert eggs.price_per_kg == pytest.approx(3.49 / 0.055, abs=0.1)
    assert eggs.price_czk == pytest.approx(34.9, abs=0.05)  # 10 x 3,49 Kč
    app = by[('Vejce z podestýlky M', True)]
    assert app.price_per_kg == pytest.approx(2.99 / 0.055, abs=0.1)
    kure = by[('Kuře bez drobů', False)]
    assert (kure.unit, kure.quantity, kure.price_czk, kure.original_price_czk) == ('kg', 1.0, 49.9, 89.9)
    assert kure.raw['confidence'] == 'medium'  # -44 % of 89,90 = 50,3 ~ 49,90
    assert all(o.valid_from == '2026-09-16' and o.valid_to == '2026-09-22' for o in offers)
    assert all(o.raw['confidence'] in ('high', 'medium', 'low') for o in offers)


def test_publitas_billa_page():
    spreads = json.loads(fixture_text('billa_spreads.json'))
    offers = publitas.parse_spreads(spreads, 'billa', '2026-09-16', '2026-09-22', 'u', 'billa-text')
    by = {(o.title, o.club): o for o in offers}
    fuet = by[('Fuet', False)]
    assert (fuet.quantity, fuet.unit, fuet.price_per_kg, fuet.price_czk) == (150.0, 'g', 299.3, 44.9)
    assert fuet.raw['confidence'] in ('high', 'medium')
    gulas = by[('Vepřový guláš porcovaný', False)]
    assert (gulas.quantity, gulas.price_per_kg) == (400.0, 174.8)
    chleb = by[('Kvasko Kmínový chléb', True)]
    assert chleb.price_per_kg == pytest.approx(98.8) and chleb.price_czk == pytest.approx(49.9)
    assert by[('Kvasko Kmínový chléb', False)].price_per_kg == pytest.approx(118.6)
    assert ('Ilustrační foto. Do vyprodání zásob.', False) not in by


def test_albert_leaflet_selection_and_fetch():
    def is_hm38(u):
        return '38hm_akcni_letak' in u

    http = FakeHttp([
        (lambda u: 'aktualni-letaky' in u, 'albert_letaky.html'),
        (is_hm38, 'albert_spreads.json'),
    ])
    p = albert.AlbertProvider(http=http, today=TODAY)
    leaflets = albert.select_leaflets(albert.parse_leaflet_list(fixture_text('albert_letaky.html')), TODAY)
    assert [l['title'] for l in leaflets] == ['Albert - 37HM_akcni_letak', 'Albert - 37SM_akcni_letak',
                                              'Albert - 38HM_acni_letak', 'Albert - 38SM_akcni_letak']
    offers = p.fetch()  # only the 38HM spreads are routed; the others fail soft
    assert offers and all(o.store == 'albert' and o.source == 'albert-text' for o in offers)
    assert all(o.valid_from == '2026-09-16' for o in offers)
    assert any('HTTP 404' in n for n in p.notes)


def test_billa_slugs_and_fetch():
    slugs = billa.parse_slugs(fixture_text('billa_tab.html'))
    assert {s['slug'] for s in slugs} == {'velky-letak-9-9-15-9-2026', 'maly-letak-9-9-15-9-2026',
                                          'velky-letak-16-9-22-9-2026', 'maly-letak-16-9-22-9-2026'}
    picked = billa.select_leaflets(slugs, TODAY)
    assert [s['slug'] for s in picked][:2] == ['velky-letak-9-9-15-9-2026', 'maly-letak-9-9-15-9-2026']
    http = FakeHttp([
        (lambda u: 'letaky-billa' in u, 'billa_tab.html'),
        (lambda u: 'velky-letak-16-9-22-9-2026/spreads.json' in u, 'billa_spreads.json'),
    ])
    offers = billa.BillaProvider(http=http, today=TODAY).fetch()
    assert offers and all(o.store == 'billa' and o.valid_to == '2026-09-22' for o in offers)
    assert billa.parse_slugs('view.publitas.com/billa-cz/velky-letak-30-12-5-1-2027/page/1')[0]['valid_from'] == '2026-12-30'


# ---- kaufland (metadata) -----------------------------------------------------
def test_kaufland_metadata_only():
    http = FakeHttp([(lambda u: 'overview' in u, 'kaufland_overview.json')])
    offers, h = kaufland.KauflandProvider(http=http, today=TODAY).run()
    assert offers == [] and h['ok'] and h['count'] == 0
    assert any(p['offer_from'] == '2026-09-16' and p['offer_to'] == '2026-09-22' for p in h['periods'])


# ---- kupi ------------------------------------------------------------------
def test_kupi_validity_mapping():
    t = dt.date(2026, 9, 15)  # Tuesday
    assert kupi.parse_validity('dnes končí', 'albert', t) == ('2026-09-15', '2026-09-15')
    assert kupi.parse_validity('zítra končí', 'albert', t) == ('2026-09-15', '2026-09-16')
    assert kupi.parse_validity('aktuální', 'albert', t) == ('2026-09-15', '2026-09-15')
    assert kupi.parse_validity('aktuální', 'lidl', t) == ('2026-09-15', '2026-09-20')
    assert kupi.parse_validity('aktuální', 'kaufland', dt.date(2026, 9, 16)) == ('2026-09-16', '2026-09-22')
    assert kupi.parse_validity('st 16. 9. – út 22. 9.', 'penny', t) == ('2026-09-16', '2026-09-22')
    assert kupi.parse_validity('platí do úterý 29. 9.', 'albert', t) == ('2026-09-15', '2026-09-29')
    assert kupi.parse_validity('', 'albert', t) == (None, None)


def test_kupi_title_matcher():
    neg = ['arasid', 'pomazank', 'dyn', 'kakaov', 'orisk', 'mandl']
    assert kupi.title_matches('Máslo Jihočeské Madeta', 'máslo', neg)
    assert not kupi.title_matches('Dýně máslová', 'máslo', neg)
    assert not kupi.title_matches('Arašídové máslo 4Slim', 'máslo', neg)
    assert kupi.title_matches('Kuřecí prsa s kostí Vodňanské kuře', 'kuřecí prsa', ['salat', 'sunk'])
    assert not kupi.title_matches('Kuřecí šunka', 'kuřecí prsa', ['salat', 'sunk'])


def test_kupi_parse_search_page():
    term = {'id': 'maslo', 'q': 'máslo', 'neg': ['arasid', 'pomazank', 'dyn', 'margarin']}
    offers = kupi.parse_search_page(fixture_text('kupi_maslo.html'), term, TODAY, 'u')
    assert offers and all(o.ingredient_id == 'maslo' and o.source == 'kupi' for o in offers)
    assert {o.title for o in offers} == {'Máslo Jihočeské Madeta', 'Máslo', 'Máslo Milkpol'}
    madeta = [o for o in offers if o.title == 'Máslo Jihočeské Madeta']
    albert_row = next(o for o in madeta if o.store == 'albert')
    assert (albert_row.price_czk, albert_row.unit, albert_row.quantity, albert_row.price_per_kg) == (
        39.9, 'g', 250.0, 159.6)
    assert (albert_row.valid_from, albert_row.valid_to) == ('2026-09-15', '2026-09-15')
    assert albert_row.raw['currently_valid'] is True and albert_row.original_price_czk == 55.61
    penny_row = next(o for o in madeta if o.store == 'penny')
    assert (penny_row.valid_from, penny_row.valid_to, penny_row.raw['note']) == (
        '2026-09-16', '2026-09-22', 'max 5 ks/osoba/den')
    billa_rows = [o for o in offers if o.store == 'billa' and o.title == 'Máslo']
    assert billa_rows and all(o.club for o in billa_rows)
    ids = [o.raw['discount_id'] for o in offers]
    assert len(ids) == len(set(ids))  # recommended + by-price sections deduped
    assert all(o.store in base.STORES for o in offers)


def test_kupi_provider_uses_terms_and_dedupes():
    http = FakeHttp([(lambda u: 'hledej' in u, 'kupi_maslo.html')])
    terms = [{'id': 'maslo', 'q': 'máslo', 'neg': ['margarin', 'dyn'], 'priority': 1},
             {'id': 'maslo2', 'q': 'máslo', 'neg': [], 'priority': 2}]
    p = kupi.KupiProvider(http=http, today=TODAY, terms=terms, max_priority=1)
    offers = p.fetch()
    assert http.requests_made == 2 and len({o.ingredient_id for o in offers}) == 2
    assert 'f=m%C3%A1slo' in http.urls[0]


def test_terms_file_loads():
    terms = kupi.load_terms()
    assert len(terms) >= 150 and all({'id', 'q', 'neg'} <= set(t) for t in terms)
    assert len({t['id'] for t in terms}) == len(terms)


# ---- registry / health ------------------------------------------------------
def test_registry_and_health_shape():
    assert registry.names() == list(registry.DEFAULT_ORDER)
    for n in registry.names():
        assert registry.get(n) is not None
    with pytest.raises(KeyError):
        registry.get('tesco')
    http = FakeHttp([])  # everything 404s
    offers, h = globus.GlobusProvider(http=http, today=TODAY).run()
    assert offers == [] and h['ok'] is False and h['source'] == 'globus' and 'HTTP 404' in h['error']
    assert set(h) >= {'source', 'ok', 'count', 'error', 'stores', 'requests', 'elapsed_s', 'notes'}

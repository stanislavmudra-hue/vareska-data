"""Kaufland - metadata only (docs/SOURCES.md section 2).

kaufland.cz sits behind a Cloudflare JS challenge and is never fetched.
The Schwarz flyer endpoint publishes the leaflet list with offer dates but
no product data, so this provider yields **no offers**; it reports the
current/next leaflet periods (used to interpret kupi's "aktuální" rows and
for the health report). Prices for Kaufland come from the ``kupi`` provider.
"""
from __future__ import annotations

from typing import Any

from .base import Offer, Provider, iso_date

OVERVIEW_URL = 'https://endpoints.leaflets.schwarz/v4/overview?client_locale=kaufland/cs-CZ&region_id=1000'


def parse_overview(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for cat in data.get('categories') or []:
        for sub in cat.get('subcategories') or []:
            for f in sub.get('flyers') or []:
                out.append({'name': f.get('name'), 'title': f.get('title'),
                            'offer_from': iso_date(f.get('offerStartDate')),
                            'offer_to': iso_date(f.get('offerEndDate')),
                            'published': iso_date(f.get('startDate')),
                            'pdf': f.get('pdfUrl'), 'subcategory': sub.get('name')})
    return out


class KauflandProvider(Provider):
    name = 'kaufland'
    stores = ('kaufland',)

    def __init__(self, http=None, today=None):
        super().__init__(http, today)
        self.periods: list[dict[str, Any]] = []

    def fetch(self) -> list[Offer]:
        data = self.http.get_json(OVERVIEW_URL)
        self.periods = parse_overview(data)
        for p in self.periods[:6]:
            self.notes.append(f'{p["title"]}: {p["offer_from"]}..{p["offer_to"]}')
        self.notes.append('metadata only - Kaufland prices come from the kupi provider')
        return []

    def run(self):
        offers, res = super().run()
        if res['error'] == 'no offers parsed' and self.periods:
            res['ok'], res['error'] = True, None
        res['periods'] = self.periods
        return offers, res


def fetch(http=None, today=None) -> list[Offer]:
    return KauflandProvider(http, today).fetch()


def health(http=None, today=None) -> dict[str, Any]:
    return KauflandProvider(http, today).health()

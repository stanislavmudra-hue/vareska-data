"""Registry of deal providers (docs/SOURCES.md decision table), one market each.

Order = merge priority: structured chain APIs first, Publitas text parsers
second, kupi.cz last (fallback for every Czech chain, sole source for
Kaufland and Tesco). ``kaufland`` is metadata only. Tesco has no direct
provider (Akamai 403 for non-browser clients).

Every provider belongs to exactly one market (``MARKET``); ``for_market(code)``
returns the providers ``--source all`` runs for that market. Chains of the
sk/pl/de/at markets other than Lidl have no provider yet ("not fetched",
see ``not_fetched()``) - their per-market price tables are published with
empty ``perKg``/``deals`` until a provider is added here.
"""
from __future__ import annotations

from typing import Any

from . import albert, billa, globus, kaufland, kupi, lidl, penny
from .. import markets

PROVIDERS: dict[str, Any] = {
    # ---- cz ----------------------------------------------------------------
    'globus': globus.GlobusProvider,   # primary, JSON API
    'lidl': lidl.LidlProvider,         # primary, JSON API
    'penny': penny.PennyProvider,      # primary (small), JSON API
    'albert': albert.AlbertProvider,   # secondary, Publitas text
    'billa': billa.BillaProvider,      # secondary, Publitas text
    'kaufland': kaufland.KauflandProvider,  # metadata only
    'kupi': kupi.KupiProvider,         # fallback, all 7 chains
    # ---- sk / pl / de / at: Lidl storefront platform per domain --------------
    'lidl_sk': lidl.provider_for('sk'),
    'lidl_pl': lidl.provider_for('pl'),
    'lidl_de': lidl.provider_for('de'),
    'lidl_at': lidl.provider_for('at'),
}

MARKET: dict[str, str] = {
    'globus': 'cz', 'lidl': 'cz', 'penny': 'cz', 'albert': 'cz', 'billa': 'cz', 'kaufland': 'cz', 'kupi': 'cz',
    'lidl_sk': 'sk', 'lidl_pl': 'pl', 'lidl_de': 'de', 'lidl_at': 'at',
}

# stores a provider delivers (for the "not fetched" list per market)
STORES_OF: dict[str, tuple[str, ...]] = {
    'globus': ('globus',), 'lidl': ('lidl',), 'penny': ('penny',), 'albert': ('albert',), 'billa': ('billa',),
    'kaufland': (), 'kupi': markets.stores_of('cz'),
    'lidl_sk': ('lidl',), 'lidl_pl': ('lidl',), 'lidl_de': ('lidl',), 'lidl_at': ('lidl',),
}

# providers run by ``--source all`` per market (kupi last so a block there
# does not hide the others)
ORDER: dict[str, tuple[str, ...]] = {
    'cz': ('globus', 'lidl', 'penny', 'albert', 'billa', 'kaufland', 'kupi'),
    'sk': ('lidl_sk',),
    'pl': ('lidl_pl',),
    'de': ('lidl_de',),
    'at': ('lidl_at',),
}
DEFAULT_ORDER = ORDER[markets.DEFAULT_MARKET]

TIER = {'globus': 'primary', 'lidl': 'primary', 'penny': 'primary', 'albert': 'secondary',
        'billa': 'secondary', 'kaufland': 'metadata', 'kupi': 'fallback',
        'lidl_sk': 'primary', 'lidl_pl': 'primary', 'lidl_de': 'primary', 'lidl_at': 'primary'}


def get(name: str):
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(f'unknown provider {name!r}; known: {", ".join(PROVIDERS)}') from None


def market_of(name: str) -> str:
    get(name)
    return MARKET.get(name, markets.DEFAULT_MARKET)


def for_market(code: str | None) -> list[str]:
    return list(ORDER.get(markets.get(code).code, ()))


def not_fetched(code: str | None) -> list[str]:
    """Stores of a market that no provider delivers yet."""
    m = markets.get(code)
    covered = {s for name in ORDER.get(m.code, ()) for s in STORES_OF.get(name, ())}
    return [s for s in m.stores if s not in covered]


def names(market: str | None = None) -> list[str]:
    return for_market(market) if market else list(DEFAULT_ORDER)

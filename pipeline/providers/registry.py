"""Registry of deal providers (docs/SOURCES.md decision table).

Order = merge priority: structured chain APIs first, Publitas text parsers
second, kupi.cz last (fallback for every chain, sole source for Kaufland and
Tesco). ``kaufland`` is metadata only. Tesco has no direct provider (Akamai
403 for non-browser clients).
"""
from __future__ import annotations

from typing import Any

from . import albert, billa, globus, kaufland, kupi, lidl, penny

PROVIDERS: dict[str, Any] = {
    'globus': globus.GlobusProvider,   # primary, JSON API
    'lidl': lidl.LidlProvider,         # primary, JSON API
    'penny': penny.PennyProvider,      # primary (small), JSON API
    'albert': albert.AlbertProvider,   # secondary, Publitas text
    'billa': billa.BillaProvider,      # secondary, Publitas text
    'kaufland': kaufland.KauflandProvider,  # metadata only
    'kupi': kupi.KupiProvider,         # fallback, all 7 chains
}

# providers run by ``--source all`` (kupi last so a block there does not hide the others)
DEFAULT_ORDER = ('globus', 'lidl', 'penny', 'albert', 'billa', 'kaufland', 'kupi')

TIER = {'globus': 'primary', 'lidl': 'primary', 'penny': 'primary', 'albert': 'secondary',
        'billa': 'secondary', 'kaufland': 'metadata', 'kupi': 'fallback'}


def get(name: str):
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(f'unknown provider {name!r}; known: {", ".join(PROVIDERS)}') from None


def names() -> list[str]:
    return list(DEFAULT_ORDER)

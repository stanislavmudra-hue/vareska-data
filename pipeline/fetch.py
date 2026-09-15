"""Entry point used by ``run_all.py`` (``python -m pipeline.fetch``).

Thin shim around :mod:`pipeline.providers.fetch`; all options are the same
(``--source all --out out/offers.json`` by default).
"""
from __future__ import annotations

import sys

from .providers.fetch import main

if __name__ == '__main__':
    sys.exit(main())

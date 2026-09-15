"""Shared test helpers: a fake Http serving saved fixtures, no network."""
from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Callable

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline.providers.http import HttpStatusError, Response  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
TODAY = dt.date(2026, 9, 15)


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
        return f.read()


class FakeHttp:
    """Routes URLs to fixture files via ``(predicate, fixture_name)`` rules."""

    def __init__(self, routes: list[tuple[Callable[[str], bool], str]], today: dt.date = TODAY):
        self.routes = routes
        self.today = today
        self.requests_made = 0
        self.cache_hits = 0
        self.log: list[dict] = []
        self.urls: list[str] = []

    def get(self, url: str, headers=None, cache=True, require_ok=True) -> Response:
        self.requests_made += 1
        self.urls.append(url)
        for pred, name in self.routes:
            if pred(url):
                return Response(url, 200, {}, fixture_text(name))
        if require_ok:
            raise HttpStatusError(404, url)
        return Response(url, 404, {}, '')

    def get_json(self, url: str, headers=None, cache=True):
        return self.get(url, headers, cache).json()

    def robots_summaries(self):
        return []

    def host_report(self):
        return {}


@pytest.fixture
def today() -> dt.date:
    return TODAY

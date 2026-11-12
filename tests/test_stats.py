"""Download statistics: reduction, chart, the cache/database/upstream layers, and the package page."""
import json
import logging
import os
import threading
import time
from datetime import date, timedelta

import pytest
import requests
from conftest import BACKENDS  # noqa: F401  (documents where the database fixture comes from)
from sqlalchemy.exc import OperationalError

from pypihub import database, pkgstats, pypimeta
from pypihub.cache import MemoryCache
from pypihub.database import PypiStats, utcnow
from pypihub.pkgstats import StatsError, StatsService, curate_daily, curate_recent, default_url, sparkline

BASE = "https://stats.test/api"
RECENT = f"{BASE}/packages/demo/recent"
OVERALL = f"{BASE}/packages/demo/overall"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, raw=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.content = raw if raw is not None else json.dumps(body).encode()

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeHttp:
    """Stands in for ``requests.Session``: a route is a response, an exception to raise, or a list of those."""

    def __init__(self):
        self.routes, self.calls, self.delay = {}, [], 0.0

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        time.sleep(self.delay)
        item = self.routes.get(url)
        if item is None:
            return FakeResponse(404)
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, Exception):
            raise item
        return item


class RecordingCache(MemoryCache):
    """A cache that remembers how long every entry was given to live."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sets = []

    def set(self, key, value, ttl):
        self.sets.append((key, ttl))
        super().set(key, value, ttl)

    def ttl_of(self, key):
        return [ttl for k, ttl in self.sets if k == key][-1]


def recent_doc(day=1200, week=8400, month=36000):
    return {"data": {"last_day": day, "last_week": week, "last_month": month}, "package": "demo", "type": "recent_downloads"}


def overall_doc(days=40, last=date(2026, 10, 31)):
    rows = []
    for i in range(days):
        day = (last - timedelta(days=days - 1 - i)).isoformat()
        rows.append({"category": "with_mirrors", "date": day, "downloads": 2000 + i})
        rows.append({"category": "without_mirrors", "date": day, "downloads": 1000 + i})
    return {"data": rows, "package": "demo", "type": "overall_downloads"}


def routes(http, **kwargs):
    http.routes[RECENT] = kwargs.get("recent", FakeResponse(body=recent_doc()))
    http.routes[OVERALL] = kwargs.get("overall", FakeResponse(body=overall_doc()))


@pytest.fixture
def service(engine, monkeypatch):
    for name, value in (("PYPIHUB_STATS", "1"), ("PYPIHUB_STATS_TTL", "21600"), ("PYPIHUB_STATS_CACHE_TTL", "3600"),
                        ("PYPIHUB_STATS_MISSING_TTL", "3600"), ("PYPIHUB_STATS_BACKOFF", "60")):
        monkeypatch.setenv(name, value)
    http = FakeHttp()
    routes(http)
    svc = StatsService(database.session_factory(engine), RecordingCache(), BASE, session=http)
    svc.fake = http
    return svc


def age_the_database_copy(service, hours):
    """Make the stored copy older, and empty the cache as if its entry had expired."""
    service._kv = RecordingCache()
    with service._session() as db:
        db.query(PypiStats).update({"fetched_at": utcnow() - timedelta(hours=hours)})
        db.commit()


# --------------------------------------------------------------------------
# Reducing the API answers
# --------------------------------------------------------------------------

def test_recent_totals_are_read():
    assert curate_recent(recent_doc(5, 50, 500)) == {"schema": 1, "last_day": 5, "last_week": 50, "last_month": 500}


@pytest.mark.parametrize("broken", [
    {}, {"data": None}, {"data": {"last_day": 1}}, {"data": {"last_day": "1", "last_week": 2, "last_month": 3}},
    {"data": {"last_day": -1, "last_week": 2, "last_month": 3}}, {"data": {"last_day": True, "last_week": 2, "last_month": 3}},
    {"data": {"last_day": 1.5, "last_week": 2, "last_month": 3}}, [], "text", None,
])
def test_a_malformed_recent_document_raises_instead_of_producing_garbage(broken):
    with pytest.raises(StatsError):
        curate_recent(broken)


def test_the_daily_series_prefers_the_one_without_mirrors_and_keeps_the_newest_30_days():
    daily = curate_daily(overall_doc(days=45))
    assert len(daily) == 30 and daily[-1] == ["2026-10-31", 1044] and daily[0][0] == "2026-10-02"
    assert [d[0] for d in daily] == sorted(d[0] for d in daily)


def test_the_daily_series_falls_back_to_mirrors_then_to_any_category():
    only_mirrors = {"data": [{"category": "with_mirrors", "date": "2026-10-01", "downloads": 7}]}
    assert curate_daily(only_mirrors) == [["2026-10-01", 7]]
    odd = {"data": [{"category": "something_new", "date": "2026-10-01", "downloads": 9}]}
    assert curate_daily(odd) == [["2026-10-01", 9]]


def test_unusable_rows_are_skipped_and_gaps_are_not_invented():
    doc = {"data": [
        {"category": "without_mirrors", "date": "2026-10-01", "downloads": 3},
        {"category": "without_mirrors", "date": "not a date", "downloads": 3},
        {"category": "without_mirrors", "date": "2026-10-04", "downloads": -5},
        {"category": "without_mirrors", "date": "2026-10-05", "downloads": "x"},
        {"category": "without_mirrors", "downloads": 3},
        "junk", None,
        {"category": "without_mirrors", "date": "2026-10-09", "downloads": 4},
    ]}
    assert curate_daily(doc) == [["2026-10-01", 3], ["2026-10-09", 4]]


@pytest.mark.parametrize("broken", [{}, {"data": None}, {"data": {"a": 1}}, [], None])
def test_a_malformed_overall_document_raises(broken):
    with pytest.raises(StatsError):
        curate_daily(broken)


def test_an_empty_overall_document_is_an_empty_series():
    assert curate_daily({"data": []}) == []


# --------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------

def _points(text):
    return [tuple(float(n) for n in pair.split(",")) for pair in text.split()]


def test_the_chart_runs_from_zero_to_the_busiest_day_and_spaces_days_by_date():
    # three days, then a gap of five days: the last point must sit far to the right, not next to the third
    daily = [["2026-10-01", 0], ["2026-10-02", 50], ["2026-10-03", 100], ["2026-10-08", 100]]
    points = _points(sparkline(daily, width=180, height=40, pad=2))
    assert points[0] == (2.0, 38.0) and points[2][1] == 2.0 and points[-1] == (178.0, 2.0)
    assert points[1][1] == 20.0
    assert points[3][0] - points[2][0] > points[2][0] - points[1][0]


def test_a_flat_or_empty_chart_is_still_drawable_or_absent():
    assert sparkline([]) == "" and sparkline([["2026-10-01", 5]]) == ""
    flat = _points(sparkline([["2026-10-01", 0], ["2026-10-02", 0]]))
    assert all(y == 38.0 for _, y in flat)
    same_day = _points(sparkline([["2026-10-01", 5], ["2026-10-01", 7]]))
    assert len(same_day) == 2  # no division by zero


def test_chart_ignores_entries_it_cannot_read():
    assert sparkline([["bad", 1], None, ["2026-10-01"], ["2026-10-01", 1], ["2026-10-02", 2]]).count(",") == 2


# --------------------------------------------------------------------------
# Where the statistics come from
# --------------------------------------------------------------------------

def test_pypistats_is_used_for_pypi_org_and_for_nothing_else(monkeypatch):
    monkeypatch.delenv("PYPIHUB_STATS_URL", raising=False)
    assert default_url("https://pypi.org/simple") == pkgstats.DEFAULT_URL
    assert default_url("https://pypi.org/simple/") == pkgstats.DEFAULT_URL
    assert default_url("https://mirror.example/simple") is None and default_url(None) is None and default_url("") is None


def test_the_url_can_be_set_explicitly(monkeypatch):
    monkeypatch.setenv("PYPIHUB_STATS_URL", "https://stats.example/api/")
    assert default_url("https://mirror.example/simple") == "https://stats.example/api"


# --------------------------------------------------------------------------
# Service: cache (shorter), database (longer), then pypistats.org
# --------------------------------------------------------------------------

def test_the_defaults_are_one_hour_in_the_cache_and_six_hours_in_the_database(engine, monkeypatch):
    for name in ("PYPIHUB_STATS_TTL", "PYPIHUB_STATS_CACHE_TTL"):
        monkeypatch.delenv(name, raising=False)
    svc = StatsService(database.session_factory(engine), MemoryCache(), BASE, session=FakeHttp())
    assert svc._ttl == 21600 and svc._cache_ttl == 3600


def test_first_view_asks_for_totals_and_the_series_stores_and_caches(service):
    result = service.get("demo")
    assert result.state == "fresh" and result.data["last_month"] == 36000 and len(result.data["daily"]) == 30
    assert [c[0] for c in service.fake.calls] == [RECENT, OVERALL]
    assert service.fake.calls[0][1]["Accept"] == "application/json" and service.fake.calls[0][2] == (2.0, 5.0)
    assert service._kv.ttl_of("stats:demo") == 3600  # one hour in the cache, although the database copy is good for six
    with service._session() as db:
        assert db.query(PypiStats).filter_by(project_key="demo").one().status == "ok"


def test_the_second_view_is_served_from_the_cache(service):
    service.get("demo")
    again = service.get("demo")
    assert again.state == "fresh" and len(service.fake.calls) == 2


def test_when_the_cache_entry_expires_the_database_answers_and_pypistats_is_not_asked(service):
    first = service.get("demo")
    service._kv = RecordingCache()  # the hour is over
    again = service.get("demo")
    assert again.state == "fresh" and again.data == first.data and len(service.fake.calls) == 2
    assert service._kv.ttl_of("stats:demo") == 3600  # and the cache is filled for another hour


def test_the_cache_never_outlives_the_freshness_of_the_database_copy(service):
    service.get("demo")
    age_the_database_copy(service, hours=5.5)  # half an hour of its six is left
    service.get("demo")
    assert len(service.fake.calls) == 2 and 1700 < service._kv.ttl_of("stats:demo") <= 1800


def test_after_six_hours_pypistats_is_asked_again(service):
    service.get("demo")
    age_the_database_copy(service, hours=6.1)
    routes(service.fake, recent=FakeResponse(body=recent_doc(day=9)))
    result = service.get("demo")
    assert result.state == "fresh" and result.data["last_day"] == 9 and len(service.fake.calls) == 4


def test_a_cache_ttl_longer_than_the_database_ttl_is_clamped(engine, monkeypatch, caplog):
    monkeypatch.setenv("PYPIHUB_STATS_TTL", "600")
    monkeypatch.setenv("PYPIHUB_STATS_CACHE_TTL", "99999")
    with caplog.at_level(logging.WARNING):
        svc = StatsService(database.session_factory(engine), RecordingCache(), BASE, session=FakeHttp())
    assert svc._cache_ttl == 600 and "PYPIHUB_STATS_CACHE_TTL" in caplog.text


def test_a_negative_cache_ttl_falls_back_to_the_default(engine, monkeypatch):
    monkeypatch.setenv("PYPIHUB_STATS_CACHE_TTL", "-5")
    assert StatsService(database.session_factory(engine), MemoryCache(), BASE, session=FakeHttp())._cache_ttl == 3600


def test_a_zero_cache_ttl_means_the_database_serves_every_view(service, monkeypatch):
    monkeypatch.setenv("PYPIHUB_STATS_CACHE_TTL", "0")
    off = StatsService(service._session, RecordingCache(), BASE, session=service.fake)
    off.get("demo")
    off.get("demo")
    assert off._kv.sets == [] and len(service.fake.calls) == 2  # fetched once (two requests), then read from the database


def test_a_refresh_that_fails_shows_the_old_copy_as_stale(service):
    service.get("demo")
    age_the_database_copy(service, hours=7)
    service.fake.routes[RECENT] = requests.ConnectionError("down")
    result = service.get("demo")
    assert result.state == "stale" and result.data["last_day"] == 1200 and result.fetched_at is not None


def test_nothing_stored_and_pypistats_down_is_unavailable_not_an_error(service):
    service.fake.routes[RECENT] = requests.ConnectionError("down")
    result = service.get("demo")
    assert result.state == "unavailable" and result.data is None
    with service._session() as db:
        assert db.query(PypiStats).count() == 0


def test_an_unknown_project_is_remembered_as_missing_and_costs_one_request(service):
    service.fake.routes.clear()
    assert service.get("demo").state == "missing"
    assert service.get("demo").state == "missing"
    assert len(service.fake.calls) == 1  # no request for the series of a project that has no totals
    assert service._kv.ttl_of("stats:demo") == 3600


def test_missing_is_checked_again_sooner_than_a_known_project(service):
    service.fake.routes.clear()
    service.get("demo")
    age_the_database_copy(service, hours=1.1)  # past the hour "missing" is remembered, far from six
    routes(service.fake)
    assert service.get("demo").state == "fresh"


def test_a_failing_series_does_not_hide_the_totals(service):
    service.fake.routes[OVERALL] = requests.ConnectionError("down")
    result = service.get("demo")
    assert result.state == "fresh" and result.data["last_week"] == 8400 and result.data["daily"] == []


@pytest.mark.parametrize("bad", [
    FakeResponse(body={"data": {"last_day": "x"}}), FakeResponse(status=500), FakeResponse(status=403),
    FakeResponse(raw=b"<html>not json</html>"), FakeResponse(raw=b"x" * (pkgstats.MAX_BODY + 1), body={}),
])
def test_unusable_answers_count_as_a_failure(service, bad):
    service.fake.routes[RECENT] = bad
    assert service.get("demo").state == "unavailable"


def test_being_rate_limited_stops_every_request_for_the_time_pypistats_asks(service):
    service.fake.routes[RECENT] = FakeResponse(status=429, headers={"Retry-After": "120"})
    assert service.get("demo").state == "unavailable"
    assert service._kv.ttl_of("stats-down") == 120
    for _ in range(5):
        assert service.get("demo").state == "unavailable"
    assert len(service.fake.calls) == 1  # one request, then silence: the limit is per address and shared by all workers


def test_a_rate_limit_without_a_usable_retry_after_uses_the_default_pause(service):
    service.fake.routes[RECENT] = FakeResponse(status=429, headers={"Retry-After": "soon"})
    service.get("demo")
    assert service._kv.ttl_of("stats-down") == 60


def test_the_pause_is_capped(service):
    service.fake.routes[RECENT] = FakeResponse(status=429, headers={"Retry-After": "86400"})
    service.get("demo")
    assert service._kv.ttl_of("stats-down") == pkgstats.MAX_BACKOFF


def test_while_paused_the_stored_copy_is_shown_as_stale(service):
    service.get("demo")
    age_the_database_copy(service, hours=7)
    service.fake.routes[RECENT] = FakeResponse(status=429)
    assert service.get("demo").state == "stale"
    calls = len(service.fake.calls)
    assert service.get("demo").state == "stale" and len(service.fake.calls) == calls


def test_a_404_does_not_start_a_pause(service):
    service.fake.routes.clear()
    service.get("demo")
    assert "stats-down" not in [k for k, _ in service._kv.sets]


def test_the_pause_can_be_switched_off(service, monkeypatch):
    monkeypatch.setenv("PYPIHUB_STATS_BACKOFF", "0")
    free = StatsService(service._session, RecordingCache(), BASE, session=service.fake)
    service.fake.routes[RECENT] = requests.ConnectionError("down")
    free.get("demo")
    free.get("demo")
    assert len(service.fake.calls) == 2


def test_the_feature_can_be_switched_off(service, monkeypatch):
    monkeypatch.setenv("PYPIHUB_STATS", "0")
    off = StatsService(service._session, RecordingCache(), BASE, session=service.fake)
    assert off.get("demo").state == "disabled" and service.fake.calls == []
    monkeypatch.setenv("PYPIHUB_STATS", "1")
    assert StatsService(service._session, RecordingCache(), None, session=service.fake).get("demo").state == "disabled"
    assert service.fake.calls == []


def test_typed_environment_values_from_envdot_are_understood(service, monkeypatch):
    """The app loads envdot, which makes os.getenv return ints, bools and lists instead of text."""
    real = os.getenv
    typed = {"PYPIHUB_STATS": 0, "PYPIHUB_STATS_TIMEOUT": ["1", "2"]}
    monkeypatch.setattr(os, "getenv", lambda name, default=None: typed.get(name, real(name, default)))
    assert StatsService(service._session, RecordingCache(), BASE, session=service.fake).enabled is False
    typed["PYPIHUB_STATS"] = True
    again = StatsService(service._session, RecordingCache(), BASE, session=service.fake)
    assert again.enabled is True and again._timeout == (1.0, 2.0)
    typed["PYPIHUB_STATS_TIMEOUT"] = "3,4"
    assert pkgstats._timeout() == (3.0, 4.0)
    typed["PYPIHUB_STATS_TIMEOUT"] = "nonsense"
    assert pkgstats._timeout() == (2.0, 5.0)


@pytest.mark.parametrize("raw, expected", [("0", False), ("false", False), ("No", False), (" OFF ", False), ("1", True),
                                           ("yes", True), ("", True), (None, True)])
def test_flag_text(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("PYPIHUB_X_FLAG", raising=False)
    else:
        monkeypatch.setenv("PYPIHUB_X_FLAG", raw)
    assert pypimeta._flag_env("PYPIHUB_X_FLAG") is expected


def test_many_simultaneous_first_views_ask_pypistats_once(service):
    service.fake.delay = 0.05
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.get("demo").state)) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["fresh"] * 12 and len(service.fake.calls) == 2


def test_a_database_failure_does_not_break_the_page(service):
    def broken():
        raise OperationalError("select", {}, Exception("database is down"))

    service._session = broken
    assert service.get("demo").state == "fresh"


@pytest.mark.parametrize("garbage", ["{not json", "[1, 2]", "null", ""])
def test_a_stored_copy_that_cannot_be_read_is_fetched_again_and_repaired(service, garbage):
    service.get("demo")
    age_the_database_copy(service, hours=0.1)  # young enough to be trusted, were it readable
    with service._session() as db:
        db.query(PypiStats).update({"data": garbage})
        db.commit()
    result = service.get("demo")
    assert result.state == "fresh" and result.data["last_month"] == 36000 and len(service.fake.calls) == 4
    age_the_database_copy(service, hours=0.1)
    assert service.get("demo").state == "fresh" and len(service.fake.calls) == 4  # the row is good again


def test_a_stored_copy_that_cannot_be_read_is_not_shown_when_pypistats_is_down(service):
    service.get("demo")
    age_the_database_copy(service, hours=7)
    with service._session() as db:
        db.query(PypiStats).update({"data": "{not json"})
        db.commit()
    service.fake.routes[RECENT] = requests.ConnectionError("down")
    result = service.get("demo")
    assert result.data is None and result.state == "unavailable"


def test_the_stored_document_is_small_and_has_no_secrets(service):
    service.get("demo")
    with service._session() as db:
        stored = db.query(PypiStats).one().data
    assert len(stored) < 3000 and set(json.loads(stored)) == {"schema", "last_day", "last_week", "last_month", "daily"}

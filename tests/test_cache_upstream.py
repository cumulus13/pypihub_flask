import json
import os
import threading
import time

import pytest
import requests

from pypihub import cache as cache_mod
from pypihub.upstream import Upstream, UpstreamError, UpstreamFile, parse_html, parse_json

REDIS_URL = os.getenv("PYPIHUB_TEST_REDIS_URL")


# --------------------------------------------------------------------------
# Cache backends (memory always, Redis when PYPIHUB_TEST_REDIS_URL is set)
# --------------------------------------------------------------------------

@pytest.fixture(params=["memory", "redis"])
def kv(request):
    if request.param == "memory":
        return cache_mod.MemoryCache(max_items=50)
    if not REDIS_URL:
        pytest.skip("set PYPIHUB_TEST_REDIS_URL to test Redis")
    store = cache_mod.RedisCache(REDIS_URL, prefix=f"test-{os.getpid()}-{time.time_ns()}:")
    return store


def test_get_set_delete(kv):
    assert kv.get("a") is None
    kv.set("a", "1", ttl=30)
    assert kv.get("a") == "1"
    kv.set("a", "2", ttl=30)
    assert kv.get("a") == "2"
    kv.delete("a")
    assert kv.get("a") is None
    assert kv.ping() is True


def test_entries_expire(kv):
    kv.set("short", "x", ttl=0.15)
    assert kv.get("short") == "x"
    time.sleep(0.3)
    assert kv.get("short") is None


def test_memory_cache_is_bounded_and_drops_least_recently_used():
    small = cache_mod.MemoryCache(max_items=3)
    for key in "abc":
        small.set(key, key, 60)
    small.get("a")  # a is now the most recently used
    small.set("d", "d", 60)
    assert small.get("b") is None and small.get("a") == "a" and small.get("d") == "d"


def test_memory_cache_survives_threads():
    shared = cache_mod.MemoryCache(max_items=100)

    def work(n):
        for i in range(500):
            shared.set(f"k{(n * 7 + i) % 150}", str(i), 30)
            shared.get(f"k{i % 150}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(shared._items) <= 100


def test_redis_outage_never_raises():
    pytest.importorskip("redis")
    broken = cache_mod.RedisCache("redis://127.0.0.1:1/0")  # nothing listens here
    assert broken.get("x") is None
    broken.set("x", "1", 10)
    broken.delete("x")
    assert broken.ping() is False


def test_make_cache_picks_the_backend():
    assert cache_mod.make_cache(None).kind == "memory"
    if REDIS_URL:
        assert cache_mod.make_cache(REDIS_URL).kind == "redis"


# --------------------------------------------------------------------------
# Upstream client with a fake HTTP session
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, json_body=None, text="", content_type="application/vnd.pypi.simple.v1+json", url="https://up.example/simple/pkg/", etag=None):
        self.status_code, self._json, self.text, self.url = status, json_body, text, url
        self.headers = {"Content-Type": content_type, **({"ETag": etag} if etag else {})}

    def json(self):
        return self._json

    def close(self):
        pass


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


JSON_BODY = {
    "files": [
        {"filename": "pkg-2.0-py3-none-any.whl", "url": "https://files.example/pkg-2.0-py3-none-any.whl",
         "hashes": {"sha256": "ab" * 32}, "requires-python": ">=3.9", "yanked": False},
        {"filename": "pkg-1.0.tar.gz", "url": "https://files.example/pkg-1.0.tar.gz", "hashes": {}, "yanked": "broken release"},
        {"filename": "", "url": "https://files.example/skipped"},
    ]
}


def test_parse_json_passes_requires_python_and_yanked_through():
    files = parse_json(JSON_BODY)
    assert [f.filename for f in files] == ["pkg-2.0-py3-none-any.whl", "pkg-1.0.tar.gz"]
    assert (files[0].sha256, files[0].requires_python, files[0].yanked) == ("ab" * 32, ">=3.9", None)
    assert (files[1].sha256, files[1].yanked) == (None, "broken release")


def test_parse_html_resolves_relative_links_and_reads_attributes():
    page = (
        '<a href="../../packages/ab/cd/pkg-1.0.tar.gz#sha256=' + "cd" * 32 + '" data-requires-python="&gt;=3.8">pkg-1.0.tar.gz</a>'
        '<a href="https://other.example/x/pkg-1.1.whl" data-yanked="">pkg-1.1.whl</a><a>no href</a>'
    )
    files = parse_html(page, "https://mirror.example/simple/pkg/")
    assert files[0].url == "https://mirror.example/packages/ab/cd/pkg-1.0.tar.gz"
    assert (files[0].sha256, files[0].requires_python, files[0].yanked) == ("cd" * 32, ">=3.8", None)
    assert files[1].url == "https://other.example/x/pkg-1.1.whl" and files[1].yanked == ""


def make(*responses, ttl="300", backoff="0"):
    os.environ["PYPIHUB_UPSTREAM_TTL"] = ttl
    os.environ["PYPIHUB_UPSTREAM_BACKOFF"] = backoff
    session = FakeSession(*responses)
    return Upstream("https://up.example/simple/", cache_mod.MemoryCache(), session=session), session


def test_files_are_fetched_once_and_then_cached():
    upstream, session = make(FakeResponse(json_body=JSON_BODY))
    first = upstream.files("Pkg_Name")
    assert len(first) == 2 and upstream.files("pkg-name") == first
    assert len(session.calls) == 1
    assert session.calls[0][0] == "https://up.example/simple/pkg-name/"  # normalised name
    assert "timeout" in session.calls[0][1] and "pypi.simple.v1+json" in session.calls[0][1]["headers"]["Accept"]


def test_html_upstream_is_supported():
    upstream, _ = make(FakeResponse(text='<a href="pkg-1.0.tar.gz">pkg-1.0.tar.gz</a>', content_type="text/html"))
    assert [f.filename for f in upstream.files("pkg")] == ["pkg-1.0.tar.gz"]


def test_not_found_is_remembered_briefly():
    upstream, session = make(FakeResponse(status=404))
    assert upstream.files("nope") is None and upstream.files("nope") is None
    assert len(session.calls) == 1


def test_server_error_without_any_earlier_copy_raises():
    upstream, _ = make(FakeResponse(status=503))
    with pytest.raises(UpstreamError):
        upstream.files("pkg")


def test_network_error_without_any_earlier_copy_raises():
    upstream, _ = make(requests.ConnectionError("down"))
    with pytest.raises(UpstreamError):
        upstream.files("pkg")


def test_stale_copy_is_served_while_the_upstream_is_down():
    upstream, session = make(FakeResponse(json_body=JSON_BODY), requests.ConnectionError("down"), ttl="0.05")
    assert len(upstream.files("pkg")) == 2
    time.sleep(0.15)  # the fresh copy expired
    assert len(upstream.files("pkg")) == 2  # served from the stale copy
    assert len(session.calls) == 2


def test_find_returns_a_single_file_or_none():
    upstream, _ = make(FakeResponse(json_body=JSON_BODY))
    assert upstream.find("pkg", "pkg-1.0.tar.gz").yanked == "broken release"
    assert upstream.find("pkg", "other.whl") is None


def test_open_refuses_non_http_urls_and_reports_errors():
    upstream, _ = make(FakeResponse(status=404))
    for bad in ("file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"):
        with pytest.raises(UpstreamError):
            upstream.open(bad)
    with pytest.raises(UpstreamError):
        upstream.open("https://files.example/missing.whl")  # answered 404


def test_upstream_file_survives_a_json_round_trip():
    original = UpstreamFile("a.whl", "https://x/a.whl", "ab" * 32, ">=3", "")
    assert Upstream._decode(Upstream._encode([original], '"tag"')) == ([original], '"tag"')
    assert Upstream._decode(Upstream._encode(None)) == (None, None)


def test_entries_written_before_etags_existed_are_still_readable():
    legacy = json.dumps([{"filename": "a.whl", "url": "https://x/a.whl", "sha256": None, "requires_python": None, "yanked": None}])
    files, etag = Upstream._decode(legacy)
    assert [f.filename for f in files] == ["a.whl"] and etag is None
    assert Upstream._decode("null") == (None, None)


def test_a_refresh_asks_whether_the_list_changed_and_a_304_keeps_the_stored_one():
    upstream, session = make(FakeResponse(json_body=JSON_BODY, etag='"v1"'), FakeResponse(status=304), ttl="0.05")
    first = upstream.files("pkg")
    time.sleep(0.15)  # the fresh copy expired, the stale one (and its ETag) did not
    assert upstream.files("pkg") == first
    assert "If-None-Match" not in session.calls[0][1]["headers"]
    assert session.calls[1][1]["headers"]["If-None-Match"] == '"v1"'
    assert upstream.files("pkg") == first and len(session.calls) == 2  # fresh again: no third request


def test_a_changed_list_replaces_the_stored_one_and_its_etag():
    changed = {"files": [{"filename": "pkg-3.0.tar.gz", "url": "https://files.example/pkg-3.0.tar.gz", "hashes": {}}]}
    upstream, session = make(FakeResponse(json_body=JSON_BODY, etag='"v1"'), FakeResponse(json_body=changed, etag='"v2"'),
                             FakeResponse(status=304), ttl="0.05")
    upstream.files("pkg")
    time.sleep(0.15)
    assert [f.filename for f in upstream.files("pkg")] == ["pkg-3.0.tar.gz"]
    time.sleep(0.15)
    upstream.files("pkg")
    assert session.calls[2][1]["headers"]["If-None-Match"] == '"v2"'


def test_a_304_nobody_asked_for_is_an_error_not_an_empty_list():
    upstream, _ = make(FakeResponse(status=304))
    with pytest.raises(UpstreamError):
        upstream.files("pkg")


def test_get_json_sends_the_etag_and_reports_304():
    upstream, session = make(FakeResponse(status=304))
    assert upstream.get_json("https://up.example/pypi/pkg/json", '"abc"') == (304, None, '"abc"')
    assert session.calls[0][1]["headers"]["If-None-Match"] == '"abc"'
    ok, _ = make(FakeResponse(json_body={"info": {}}, etag='"new"'))
    assert ok.get_json("https://up.example/pypi/pkg/json") == (200, {"info": {}}, '"new"')
    gone, _ = make(FakeResponse(status=404))
    assert gone.get_json("https://up.example/pypi/pkg/json", '"abc"') == (404, None, None)


@pytest.mark.skipif(not REDIS_URL, reason="needs PYPIHUB_TEST_REDIS_URL")
def test_two_workers_share_the_upstream_cache_through_redis():
    prefix = f"share-{time.time_ns()}:"
    first_session, second_session = FakeSession(FakeResponse(json_body=JSON_BODY)), FakeSession(FakeResponse(status=500))
    os.environ["PYPIHUB_UPSTREAM_TTL"] = "300"
    one = Upstream("https://up.example/simple", cache_mod.RedisCache(REDIS_URL, prefix=prefix), session=first_session)
    two = Upstream("https://up.example/simple", cache_mod.RedisCache(REDIS_URL, prefix=prefix), session=second_session)
    assert len(one.files("pkg")) == 2
    assert len(two.files("pkg")) == 2 and second_session.calls == []  # answered from what worker one stored


# --------------------------------------------------------------------------
# Circuit breaker: a failing upstream is not asked again for a moment
# --------------------------------------------------------------------------

def test_after_a_failure_the_network_is_skipped_for_the_backoff_period():
    upstream, session = make(requests.ConnectionError("down"), FakeResponse(json_body=JSON_BODY), backoff="0.3")
    with pytest.raises(UpstreamError):
        upstream.files("pkg")
    for _ in range(5):  # answered instantly, without touching the network
        with pytest.raises(UpstreamError):
            upstream.files("pkg")
    assert len(session.calls) == 1
    time.sleep(0.4)
    assert len(upstream.files("pkg")) == 2 and len(session.calls) == 2


def test_server_errors_trip_the_breaker_but_not_found_does_not():
    upstream, session = make(FakeResponse(status=503), backoff="5")
    with pytest.raises(UpstreamError):
        upstream.files("a")
    with pytest.raises(UpstreamError):
        upstream.files("b")
    assert len(session.calls) == 1
    missing, session = make(FakeResponse(status=404), backoff="5")
    assert missing.files("a") is None and missing.files("b") is None and len(session.calls) == 2


def test_the_breaker_still_lets_stale_copies_through():
    upstream, _ = make(FakeResponse(json_body=JSON_BODY), requests.ConnectionError("down"), ttl="0.05", backoff="5")
    assert len(upstream.files("pkg")) == 2
    time.sleep(0.15)
    assert len(upstream.files("pkg")) == 2  # fetch failed, tripped, stale copy served
    assert len(upstream.files("pkg")) == 2  # breaker open, stale copy served without a request


def test_downloads_respect_the_breaker():
    upstream, session = make(requests.ConnectionError("down"), backoff="5")
    for _ in range(3):
        with pytest.raises(UpstreamError):
            upstream.open("https://files.example/x.whl")
    assert len(session.calls) == 1


def test_a_hung_upstream_costs_one_timeout_not_one_per_request():
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held = []
    threading.Thread(target=lambda: [held.append(server.accept()) for _ in range(16)], daemon=True).start()
    os.environ.update(PYPIHUB_UPSTREAM_TIMEOUT="0.3,0.3", PYPIHUB_UPSTREAM_BACKOFF="5", PYPIHUB_UPSTREAM_TTL="300")
    try:
        upstream = Upstream(f"http://127.0.0.1:{server.getsockname()[1]}/simple", cache_mod.MemoryCache())
        started = time.perf_counter()
        with pytest.raises(UpstreamError):
            upstream.files("pkg")  # accepts the connection, never answers
        first = time.perf_counter() - started
        started = time.perf_counter()
        for _ in range(20):
            with pytest.raises(UpstreamError):
                upstream.files("pkg")
        assert first < 2.0 and time.perf_counter() - started < 0.2
    finally:
        server.close()
        os.environ["PYPIHUB_UPSTREAM_TIMEOUT"] = "3.05,20"

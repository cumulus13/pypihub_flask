"""Project details copied from the upstream: reduction, rendering, storage and the package page."""
import json
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import BACKENDS  # noqa: F401  (documents where the database fixture comes from)

from pypihub import database, pypimeta, readme, web
from pypihub.cache import MemoryCache
from pypihub.database import PypiMeta, utcnow
from pypihub.pypimeta import MetaResult, MetaService, curate, group_classifiers, safe_url, split_requirements
from pypihub.upstream import UpstreamError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pypi_requests.json").read_text())


def document(**info):
    base = {
        "name": "Demo-Pkg", "version": "2.0", "summary": "A demo package", "description": "# Demo\n\nHello **world**",
        "description_content_type": "text/markdown", "author": "Ada Lovelace", "author_email": None,
        "maintainer": None, "maintainer_email": None, "license": "MIT", "license_expression": None,
        "home_page": "https://demo.example", "project_urls": {"Source": "https://git.example/demo", "Evil": "javascript:alert(1)"},
        "classifiers": ["Development Status :: 5 - Production/Stable", "Programming Language :: Python :: 3",
                        "Programming Language :: Python :: 3.12"],
        "keywords": "demo, test", "requires_python": ">=3.9", "requires_dist": ["requests>=2", "pytest; extra == 'test'"],
        "yanked": False, "yanked_reason": None,
    }
    base.update(info)
    return {
        "info": base,
        "urls": [{"filename": "demo_pkg-2.0-py3-none-any.whl", "size": 1234, "upload_time_iso_8601": "2026-03-01T10:00:00.000000Z",
                  "digests": {"sha256": "ab" * 32}, "packagetype": "bdist_wheel", "python_version": "py3", "requires_python": ">=3.9", "yanked": False}],
        "releases": {
            "1.0": [{"upload_time_iso_8601": "2025-01-01T00:00:00Z", "yanked": False}],
            "2.0": [{"upload_time_iso_8601": "2026-03-01T10:00:00Z", "yanked": False}],
            "2.1rc1": [{"upload_time_iso_8601": "2026-04-01T10:00:00Z", "yanked": False}],
            "1.5": [{"upload_time_iso_8601": "2025-06-01T00:00:00Z", "yanked": True}, {"upload_time_iso_8601": "2025-06-01T00:00:01Z", "yanked": True}],
            "weird_version!": [],
        },
        "vulnerabilities": [{"id": "PYSEC-1", "summary": None, "details": "Bad things", "link": "https://osv.dev/PYSEC-1", "fixed_in": ["1.0.1"], "withdrawn": None},
                            {"id": "PYSEC-2", "summary": "withdrawn", "withdrawn": "2025-01-01"}],
    }


# --------------------------------------------------------------------------
# Reducing a JSON API document
# --------------------------------------------------------------------------

def test_a_real_document_is_reduced_to_the_fields_the_page_uses():
    data = curate(FIXTURE, with_releases=True)
    info = FIXTURE["info"]
    assert data["name"] == "requests" and data["version"] == info["version"] and data["summary"] == info["summary"]
    assert data["description"] == info["description"] and data["description_html"] and "<script" not in data["description_html"]
    assert data["requires_python"] == info["requires_python"] and data["classifiers"] == info["classifiers"]
    assert data["files"][0]["filename"] == FIXTURE["urls"][0]["filename"] and len(data["files"][0]["sha256"]) == 64
    assert data["releases"][0]["version"] == info["version"] and len(data["releases"]) == len(FIXTURE["releases"])
    assert len(json.dumps(data)) < len(json.dumps(FIXTURE)) * 2  # no ballooning
    json.dumps(data)  # and it must be storable


def test_curate_fields():
    data = curate(document(), with_releases=True)
    assert data["authors"] == [{"name": "Ada Lovelace", "email": None}]
    assert data["license"] == "MIT" and data["keywords"] == ["demo", "test"]
    assert data["project_urls"] == {"Homepage": "https://demo.example", "Source": "https://git.example/demo"}
    assert "Evil" not in data["project_urls"]  # javascript: links are never kept


def test_releases_are_sorted_newest_first_and_flagged():
    releases = curate(document(), with_releases=True)["releases"]
    versions = [r["version"] for r in releases]
    assert versions[:4] == ["2.1rc1", "2.0", "1.5", "1.0"] and "weird_version!" not in versions[:4]
    by = {r["version"]: r for r in releases}
    assert by["2.1rc1"]["pre"] and not by["2.0"]["pre"]
    assert by["1.5"]["yanked"] and by["1.5"]["files"] == 2 and not by["1.0"]["yanked"]
    assert by["1.5"]["uploaded"] == "2025-06-01T00:00:00Z"


def test_a_release_document_has_no_history():
    assert "releases" not in curate(document(), with_releases=False)


@pytest.mark.parametrize(
    "info, expected",
    [({"license_expression": "Apache-2.0", "license": "ignored"}, "Apache-2.0"),
     ({"license": "BSD-3-Clause"}, "BSD-3-Clause"),
     ({"license": "Copyright (c) 2020\nPermission is hereby granted..."}, "Copyright (c) 2020…"),
     ({"license": None, "classifiers": ["License :: OSI Approved :: MIT License"]}, "MIT License"),
     ({"license": None, "classifiers": []}, "")],
)
def test_license_choice(info, expected):
    assert curate(document(**info), False)["license"] == expected


def test_people_are_read_from_names_and_addresses():
    one = curate(document(author=None, author_email="Ada <ada@example.org>, Bob <bob@example.org>"), False)["authors"]
    assert one == [{"name": "Ada", "email": "ada@example.org"}, {"name": "Bob", "email": "bob@example.org"}]
    assert curate(document(author="A, B", author_email=None), False)["authors"] == [{"name": "A", "email": None}, {"name": "B", "email": None}]
    junk = curate(document(author=None, author_email="not an address"), False)["authors"]
    assert all(p["email"] is None for p in junk)


@pytest.mark.parametrize("url, ok", [("https://x.example/a", True), ("http://x.example", True), ("javascript:alert(1)", False),
                                     ("data:text/html,x", False), ("ftp://x.example", False), ("//x.example", False), ("", False),
                                     (None, False), ("https://" + "a" * 600, False)])
def test_safe_url(url, ok):
    assert (safe_url(url) is not None) is ok


def test_vulnerabilities_skip_withdrawn_entries_and_tolerate_missing_fields():
    found = curate(document(), True)["vulnerabilities"]
    assert found == [{"id": "PYSEC-1", "summary": "Bad things", "link": "https://osv.dev/PYSEC-1", "fixed_in": ["1.0.1"]}]


def test_requirements_and_classifiers_are_grouped():
    assert split_requirements(["a", "b; extra == 'x'", "c; extra == \"x\"", "d; extra == 'a'"]) == \
        {"": ["a"], "a": ["d; extra == 'a'"], "x": ["b; extra == 'x'", 'c; extra == "x"']}
    assert group_classifiers(["A :: B", "A :: C", "D"]) == {"A": ["B", "C"], "D": ["D"]}


@pytest.mark.parametrize("broken", [{}, {"info": None}, {"info": []}])
def test_a_malformed_document_raises_instead_of_producing_garbage(broken):
    with pytest.raises((KeyError, TypeError, AttributeError)):
        curate(broken, True)


# --------------------------------------------------------------------------
# README rendering
# --------------------------------------------------------------------------

def test_markdown_rst_and_plain_text():
    assert "<strong>b</strong>" in readme.render("**b**", "text/markdown; charset=UTF-8; variant=GFM")
    assert "<em>x</em>" in readme.render("T\n=\n\n*x*", "text/x-rst")
    assert readme.render("a <b>b</b>", "text/plain") == "a &lt;b&gt;b&lt;/b&gt;"
    assert "<b>" not in readme.render("a <b>b</b>", None) and "&lt;b&gt;" in readme.render("a <b>b</b>", None)  # no type: reST, never raw HTML


def test_unparsable_rst_falls_back_to_plain_text_like_pypi():
    html = readme.render("T\n=\n.. no-such-directive:: x\n", "text/x-rst")
    assert html and "no-such-directive" in html


@pytest.mark.parametrize(
    "payload",
    ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "[x](javascript:alert(1))", "<iframe src=//evil.example></iframe>",
     "<svg onload=alert(1)>", "<a href='data:text/html,<script>alert(1)</script>'>x</a>", "<style>*{display:none}</style>",
     "<form action=//evil.example><input name=p></form>", "<object data=//evil.example></object>", "<meta http-equiv=refresh content='0;url=//evil.example'>",
     "<base href=//evil.example>", "<link rel=stylesheet href=//evil.example/x.css>", "<div style='position:fixed'>x</div>",
     "<a href=\"JaVaScRiPt:alert(1)\">x</a>", "<math><mi xlink:href='javascript:alert(1)'>x</mi></math>"],
)
def test_hostile_markup_never_survives(payload):
    html = readme.render(f"before\n\n{payload}\n\nafter", "text/markdown") or ""
    lowered = html.lower()
    for bad in ("<script", "onerror", "onload", "javascript:", "<iframe", "<svg", "<style", "<form", "<object", "<meta", "<base", "<link", "style=", "data:text"):
        assert bad not in lowered, (bad, html)
    assert "before" in html and "after" in html


def test_images_load_lazily_without_a_referrer():
    html = readme.render("![shot](https://img.example/a.png)", "text/markdown")
    assert 'loading="lazy"' in html and 'referrerpolicy="no-referrer"' in html and 'src="https://img.example/a.png"' in html


@pytest.mark.parametrize("raw", [None, "", "   \n"])
def test_empty_descriptions_render_to_nothing(raw):
    assert readme.render(raw, "text/markdown") is None


def test_oversized_descriptions_are_not_rendered():
    assert readme.render("x " * (readme.MAX_RENDER_CHARS), "text/markdown") is None


def test_without_readme_renderer_installed_nothing_is_rendered(monkeypatch):
    for name in ("readme_renderer", "readme_renderer.markdown", "readme_renderer.rst", "readme_renderer.txt"):
        monkeypatch.setitem(sys.modules, name, None)  # makes `import` raise ImportError
    assert readme.render("**x**", "text/markdown") is None and readme.available() is False


# --------------------------------------------------------------------------
# Service: cache, database, upstream
# --------------------------------------------------------------------------

class FakeUpstream:
    def __init__(self):
        self.docs, self.calls, self.down, self.delay = {}, [], False, 0.0

    def json_url(self, project, version=None):
        return f"https://pypi.test/pypi/{project}/{version}/json" if version else f"https://pypi.test/pypi/{project}/json"

    def get_json(self, url):
        self.calls.append(url)
        time.sleep(self.delay)
        if self.down:
            raise UpstreamError("down")
        if url not in self.docs:
            return 404, None
        return 200, self.docs[url]


@pytest.fixture
def service(engine, monkeypatch):
    monkeypatch.setenv("PYPIHUB_META", "1")
    monkeypatch.setenv("PYPIHUB_META_TTL", "300")
    upstream = FakeUpstream()
    upstream.docs["https://pypi.test/pypi/demo/json"] = document()
    upstream.docs["https://pypi.test/pypi/demo/2.0/json"] = document(version="2.0")
    svc = MetaService(database.session_factory(engine), MemoryCache(), upstream)
    svc.fake = upstream
    return svc


def test_first_view_fetches_stores_and_remembers(service):
    result = service.get("demo")
    assert result.state == "fresh" and result.data["version"] == "2.0" and len(service.fake.calls) == 1
    with service._session() as s:
        row = s.query(PypiMeta).one()
    assert (row.project_key, row.version, row.status) == ("demo", "", "ok") and json.loads(row.data)["name"] == "Demo-Pkg"
    assert service.get("demo").state == "fresh" and len(service.fake.calls) == 1  # from the cache


def test_a_cold_cache_is_refilled_from_the_database_not_the_upstream(service):
    service.get("demo")
    service._kv = MemoryCache()  # a restart, or another replica with an empty cache
    assert service.get("demo").data["name"] == "Demo-Pkg" and len(service.fake.calls) == 1


def test_a_release_is_stored_separately_from_the_project(service):
    service.get("demo")
    release = service.get("demo", "2.0")
    assert release.state == "fresh" and "releases" not in release.data
    with service._session() as s:
        assert sorted(r.version for r in s.query(PypiMeta)) == ["", "2.0"]


def test_an_expired_copy_is_refreshed(service, monkeypatch):
    service.get("demo")
    service._kv = MemoryCache()
    with service._session() as s:
        s.query(PypiMeta).update({"fetched_at": utcnow() - timedelta(hours=7)})
        s.commit()
    service.fake.docs["https://pypi.test/pypi/demo/json"] = document(version="3.0")
    assert service.get("demo").data["version"] == "3.0" and len(service.fake.calls) == 2


def test_when_the_refresh_fails_the_old_copy_is_shown_as_stale(service):
    service.get("demo")
    service._kv = MemoryCache()
    with service._session() as s:
        s.query(PypiMeta).update({"fetched_at": utcnow() - timedelta(hours=7)})
        s.commit()
    service.fake.down = True
    result = service.get("demo")
    assert result.state == "stale" and result.data["name"] == "Demo-Pkg" and result.fetched_at is not None


def test_nothing_stored_and_upstream_down_is_unavailable_not_an_error(service):
    service.fake.down = True
    result = service.get("demo")
    assert (result.state, result.data) == ("unavailable", None)


def test_unknown_projects_are_remembered_as_missing(service):
    assert service.get("nothing").state == "missing" and service.get("nothing").state == "missing"
    assert len(service.fake.calls) == 1
    with service._session() as s:
        assert s.query(PypiMeta).one().status == "missing"


def test_missing_expires_sooner_and_is_checked_again(service, monkeypatch):
    service.get("nothing")
    service._kv = MemoryCache()
    with service._session() as s:
        s.query(PypiMeta).update({"fetched_at": utcnow() - timedelta(hours=2)})
        s.commit()
    service.fake.docs["https://pypi.test/pypi/nothing/json"] = document(name="nothing")
    assert service.get("nothing").state == "fresh"


def test_the_feature_can_be_switched_off(service, monkeypatch):
    monkeypatch.setenv("PYPIHUB_META", "0")
    off = MetaService(service._session, MemoryCache(), service.fake)
    assert off.get("demo").state == "disabled" and service.fake.calls == []
    plain_mirror = type("U", (), {"json_url": lambda self, *a, **k: None})()
    monkeypatch.setenv("PYPIHUB_META", "1")
    assert MetaService(service._session, MemoryCache(), plain_mirror).get("demo").state == "disabled"


def test_many_simultaneous_first_views_fetch_once(service):
    service.fake.delay = 0.05
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.get("demo").state)) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["fresh"] * 10 and len(service.fake.calls) == 1


def test_a_database_failure_does_not_break_the_page(service, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def boom(*a, **k):
        raise OperationalError("select", {}, Exception("database is gone"))

    monkeypatch.setattr(service, "_session", boom)
    assert service.get("demo").state == "fresh"  # served straight from the upstream


def test_a_malformed_upstream_document_counts_as_a_failure(service):
    service.fake.docs["https://pypi.test/pypi/bad/json"] = {"unexpected": True}
    assert service.get("bad").state == "unavailable"


def test_a_long_readme_survives_every_database(service):
    """MySQL's TEXT holds 64 kB; the column must be LONGTEXT there."""
    big = ("Paragraph with *words* and `code`.\n\n" * 12000)
    assert len(big) > 400_000
    service.fake.docs["https://pypi.test/pypi/big/json"] = document(name="big", description=big, description_content_type="text/markdown")
    first = service.get("big")
    service._kv = MemoryCache()
    again = service.get("big")
    assert again.data["description"] == first.data["description"] == big[: pypimeta.MAX_DESCRIPTION] and again.data["description_html"]


def test_unicode_survives_every_database(service):
    text = "日本語の説明 — emoji 🎉 — Ünïcödé — العربية"
    service.fake.docs["https://pypi.test/pypi/uni/json"] = document(name="uni", summary=text, description=text, description_content_type="text/plain")
    service.get("uni")
    service._kv = MemoryCache()
    assert service.get("uni").data["summary"] == text


def test_metaresult_round_trips_through_json():
    original = MetaResult({"a": 1}, "stale", utcnow())
    again = MetaResult.load(original.dump())
    assert (again.data, again.state, again.fetched_at) == (original.data, original.state, original.fetched_at)

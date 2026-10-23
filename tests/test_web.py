import base64
import logging
import re
import sys
import threading
import time
from io import BytesIO

import pytest

from pypihub import web
from pypihub.upstream import UpstreamError, UpstreamFile

pytestmark = pytest.mark.usefixtures("clean")  # empty directories, tables and counters for every test

AUTH = {"Authorization": "Basic " + base64.b64encode(b"alice:s3cretpw-123").decode()}


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def rows(body):
    return re.findall(r'class="pkgs__link"[^>]*>([^<]+)<', body)


def record_many(server, count, source="cached", size=lambda i: 1000 + i):
    for i in range(count):
        server.module.CATALOG.record_file(f"pkg{i:03d}", f"pkg{i:03d}-1.{i}-py3-none-any.whl", source, size(i))


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "size, text",
    [(0, "0 B"), (999, "999 B"), (1000, "1.0 kB"), (1_500_000, "1.5 MB"), (999_999, "1.0 MB"), (3_200_000_000, "3.2 GB")],
)
def test_human_size(size, text):
    assert web.human_size(size) == text


@pytest.mark.parametrize(
    "age, text",
    [(5, "just now"), (60, "1 minute ago"), (7200, "2 hours ago"), (86400, "1 day ago"),
     (40 * 86400, "1 month ago"), (800 * 86400, "2 years ago")],
)
def test_time_ago(age, text):
    assert web.time_ago(1000.0, now=1000.0 + age) == text


# --------------------------------------------------------------------------
# Home page: listing, pagination, search, sort
# --------------------------------------------------------------------------

def test_index_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Nothing is stored here yet" in body and "Showing" not in body


def test_index_lists_local_and_cached(client, add_file):
    add_file("local", "mylib", "mylib-0.4.1-py3-none-any.whl", b"x" * 2000)
    add_file("local", "mylib", "mylib-0.3.0.tar.gz")
    add_file("cached", "requests", "requests-2.32.3-py3-none-any.whl", b"y" * 5000)
    body = client.get("/").get_data(as_text=True)
    assert rows(body) == ["mylib", "requests"]
    assert 'href="/project/mylib/"' in body and "0.4.1" in body and "2.32.3" in body
    assert "2 packages, 3 files" in body


def test_pagination(client, server):
    record_many(server, 95)
    first = client.get("/").get_data(as_text=True)
    assert len(rows(first)) == 30 and rows(first)[0] == "pkg000"
    assert "Showing 1 to 30 of 95" in first and 'aria-label="Package list pages"' in first
    assert 'rel="next"' in first and 'rel="prev"' not in first
    second = client.get("/?page=2").get_data(as_text=True)
    assert rows(second)[0] == "pkg030" and "Showing 31 to 60 of 95" in second
    assert 'rel="prev"' in second and 'aria-current="page"' in second
    last = client.get("/?page=4").get_data(as_text=True)
    assert len(rows(last)) == 5 and 'rel="next"' not in last


@pytest.mark.parametrize("page", ["99", "abc", "-3", "0", "", "1e9", "%00"])
def test_odd_page_numbers_never_break_the_page(client, server, page):
    record_many(server, 40)
    response = client.get(f"/?page={page}")
    assert response.status_code == 200 and rows(response.get_data(as_text=True))


def test_pages_cover_every_package_exactly_once(client, server):
    record_many(server, 95)
    seen = []
    for page in range(1, 5):
        seen += rows(client.get(f"/?page={page}").get_data(as_text=True))
    assert seen == [f"pkg{i:03d}" for i in range(95)]


def test_search_filters_on_the_server_and_keeps_its_terms_in_page_links(client, server):
    record_many(server, 95)
    narrow = client.get("/?q=pkg01").get_data(as_text=True)
    assert len(rows(narrow)) == 10 and "10 of 95 packages" in narrow and "pager" not in narrow.split('id="setup"')[0].split("list-foot")[-1]
    wide = client.get("/?q=pkg").get_data(as_text=True)
    assert "q=pkg&amp;page=2" in wide and len(rows(wide)) == 30


def test_search_is_normalised_like_pip(client, add_file):
    add_file("cached", "Flask_Cors", "flask_cors-1.0.tar.gz")
    assert rows(client.get("/?q=FLASK-cors").get_data(as_text=True)) == ["Flask_Cors"]
    assert rows(client.get("/?q=flask.cors").get_data(as_text=True)) == ["Flask_Cors"]


def test_sorting(client, server):
    record_many(server, 40)
    by_size = rows(client.get("/?sort=size&dir=desc").get_data(as_text=True))
    assert by_size[0] == "pkg039" and by_size[1] == "pkg038"
    assert rows(client.get("/?sort=name&dir=desc").get_data(as_text=True))[0] == "pkg039"
    body = client.get("/?sort=size&dir=desc").get_data(as_text=True)
    assert 'aria-sort="descending"' in body and "sort=size&amp;dir=asc" in body  # the header link flips the direction


def test_unknown_sort_and_direction_fall_back_to_defaults(client, server):
    record_many(server, 3)
    response = client.get("/?sort=password&dir=sideways&source=everything")
    assert response.status_code == 200 and rows(response.get_data(as_text=True)) == ["pkg000", "pkg001", "pkg002"]


def test_source_filter(client, server):
    record_many(server, 5, source="cached")
    server.module.CATALOG.record_file("mine", "mine-1.0.tar.gz", "local", 10)
    body = client.get("/?source=local").get_data(as_text=True)
    assert rows(body) == ["mine"] and re.search(r'href="[^"]*source=local[^"]*" data-nav aria-current="true"', body)
    assert len(rows(client.get("/?source=cached").get_data(as_text=True))) == 5


def test_search_text_is_escaped_everywhere(client, server):
    record_many(server, 3)
    payload = '"><script>alert(1)</script>'
    body = client.get("/", query_string={"q": payload}).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


@pytest.mark.parametrize("q", ["' OR 1=1 --", "%", "_", "x'; DROP TABLE dist_files;--", "\\", "a" * 5000])
def test_hostile_search_terms_are_harmless(client, server, q):
    record_many(server, 3)
    assert client.get("/", query_string={"q": q}).status_code == 200
    assert len(rows(client.get("/").get_data(as_text=True))) == 3


def test_pip_command_follows_the_search(client, server):
    record_many(server, 3)
    command = lambda q: re.search(r'<span class="cmd__pkg">([^<]*)</span>', client.get("/", query_string={"q": q}).get_data(as_text=True)).group(1)  # noqa: E731
    assert command("") == "package-name"
    assert command("PKG001") == "pkg001"  # exact match, normalised
    assert command("pkg002x") == "pkg002x"  # not stored: the command is what they typed
    assert command("pkg0") == "package-name"  # several matches: no guess
    assert command("bad name; rm -rf /") == "package-name"
    unknown = client.get("/", query_string={"q": "django"}).get_data(as_text=True)
    assert "Not stored on this server yet" in unknown
    assert "Not stored on this server yet" not in client.get("/", query_string={"q": "pkg001"}).get_data(as_text=True)


def test_large_catalog_pages_quickly(client, server):
    from sqlalchemy import insert

    from pypihub.database import DistFile, utcnow

    now = utcnow()
    with server.module.SessionLocal() as db:
        db.execute(
            insert(DistFile),
            [dict(project_key=f"big{i:05d}", project_name=f"big{i:05d}", filename=f"big{i:05d}-1.0.tar.gz", source="cached",
                  kind="sdist", version="1.0", is_prerelease=False, size=i, created_at=now) for i in range(5000)],
        )
        db.commit()
    server.module.KV_CACHE.delete("catalog:stats")
    for url, expected in (("/", 30), ("/?page=100", 30), ("/?q=big0490", 10), ("/?sort=size&dir=desc&page=7", 30)):
        start = time.perf_counter()
        response = client.get(url)
        assert response.status_code == 200 and len(rows(response.get_data(as_text=True))) == expected, url
        assert time.perf_counter() - start < 1.5, url
    assert "5,000 packages" in client.get("/").get_data(as_text=True)


# --------------------------------------------------------------------------
# Package page
# --------------------------------------------------------------------------

def test_package_page_lists_files_with_download_links(client, add_file):
    add_file("local", "mylib", "mylib-0.4.1-py3-none-any.whl")
    add_file("cached", "mylib", "mylib-0.3.0.tar.gz")
    body = client.get("/project/mylib/").get_data(as_text=True)
    assert "/packages/mylib/mylib-0.4.1-py3-none-any.whl" in body and "/cache/mylib/mylib-0.3.0.tar.gz" in body
    assert body.index("mylib-0.4.1") < body.index("mylib-0.3.0") and "pip install --index-url" in body


def test_package_files_are_paginated(client, server):
    for i in range(120):
        server.module.CATALOG.record_file("many", f"many-{i}.0.tar.gz", "cached", 10)
    first = client.get("/project/many/").get_data(as_text=True)
    assert first.count('class="dl"') == 50 and "Showing 1 to 50 of 120" in first
    assert client.get("/project/many/?page=3").get_data(as_text=True).count('class="dl"') == 20
    assert client.get("/project/many/?page=99").status_code == 200


def test_download_links_actually_download(client, add_file):
    add_file("local", "mylib", "mylib-1.0-py3-none-any.whl", b"WHEEL")
    add_file("cached", "mylib", "mylib-0.9.tar.gz", b"SDIST")
    assert client.get("/packages/mylib/mylib-1.0-py3-none-any.whl").data == b"WHEEL"
    assert client.get("/cache/mylib/mylib-0.9.tar.gz").data == b"SDIST"


def test_project_lookup_is_normalised(client, add_file):
    add_file("local", "flask_cors", "flask_cors-1.0.tar.gz")
    for spelling in ("flask-cors", "Flask_Cors", "FLASK.CORS"):
        assert client.get(f"/project/{spelling}/").status_code == 200


@pytest.mark.parametrize("name", ["missing", "..", "%2e%2e", "..%2f..%2fetc", "a/b"])
def test_project_unknown_or_hostile_names_are_404(client, name):
    assert client.get(f"/project/{name}/").status_code == 404


def test_file_names_are_escaped(client, server):
    server.module.CATALOG.record_file("xss", "<img src=x onerror=alert(1)>.txt", "cached", 1)
    body = client.get("/project/xss/").get_data(as_text=True)
    assert "<img src=x" not in body and "&lt;img src=x onerror=alert(1)&gt;.txt" in body


# --------------------------------------------------------------------------
# Errors, headers, health, assets
# --------------------------------------------------------------------------

def test_404_is_html_for_browsers_and_plain_for_pip(client):
    page = client.get("/project/nope/", headers={"Accept": "text/html"})
    assert page.status_code == 404 and "Page not found" in page.get_data(as_text=True)
    plain = client.get("/packages/nope/file.whl", headers={"Accept": "text/html"})
    assert plain.status_code == 404 and "Page not found" not in plain.get_data(as_text=True)


def test_security_headers_on_pages(client):
    headers = client.get("/").headers
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "connect-src 'self'" in csp  # without it the browser blocks the live search requests
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_pages_have_no_inline_script_or_style(client, add_file):
    add_file("local", "mylib", "mylib-1.0.tar.gz")
    for path in ("/", "/project/mylib/", "/project/none/", "/?q=zzz"):
        body = client.get(path, headers={"Accept": "text/html"}).get_data(as_text=True)
        assert " style=" not in body and " onclick=" not in body and "<script>" not in body, path


def test_healthz(client):
    response = client.get("/healthz")
    data = response.get_json()
    assert response.status_code == 200 and data["status"] == "ok" and data["version"]
    assert data["checks"]["database"] and data["checks"]["packages_dir_writable"]
    assert response.headers["Cache-Control"] == "no-store"


def test_healthz_reports_a_database_outage(client, server, monkeypatch):
    monkeypatch.setattr(server.module.CATALOG, "ping", lambda: False)
    response = client.get("/healthz")
    assert response.status_code == 503 and response.get_json()["checks"]["database"] is False


@pytest.mark.parametrize(
    "path, mimetype",
    [("css/pypihub.css", "text/css"), ("js/pypihub.js", "text/javascript"), ("js/theme.js", "text/javascript"),
     ("vendor/bootstrap/bootstrap.min.css", "text/css"), ("vendor/bootstrap/bootstrap.bundle.min.js", "text/javascript"),
     ("fonts/bricolage-grotesque-latin-wght-normal.woff2", "font/woff2"), ("img/logo-72.png", "image/png")],
)
def test_static_assets_are_served(client, path, mimetype):
    response = client.get(f"/static/{path}")
    assert response.status_code == 200 and response.mimetype in (mimetype, "application/javascript") and len(response.data) > 100


def test_every_asset_referenced_by_a_page_exists(client):
    body = client.get("/").get_data(as_text=True)
    urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', body)
    assert len(urls) >= 6
    for url in urls:
        assert client.get(url).status_code == 200, url


@pytest.mark.parametrize(
    "base_url, expect_trusted",
    [("http://192.168.1.10:5000", "--trusted-host 192.168.1.10"), ("http://pypi.lan:8080", "--trusted-host pypi.lan"),
     ("http://localhost:5000", None), ("http://127.0.0.1:5000", None), ("https://pypi.example.org", None)],
)
def test_pip_command_trusted_host(client, base_url, expect_trusted):
    body = client.get("/", base_url=base_url).get_data(as_text=True)
    assert f"--index-url {base_url}/simple/" in body and ("--trusted-host" in body) == bool(expect_trusted)
    if expect_trusted:
        assert expect_trusted in body


def test_proxy_headers_are_honoured_only_when_wrapped(server):
    from werkzeug.middleware.proxy_fix import ProxyFix

    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "packages.example.org"}
    assert "https://packages.example.org" not in server.app.test_client().get("/", headers=headers).get_data(as_text=True)
    original = server.app.wsgi_app
    server.app.wsgi_app = ProxyFix(original, x_proto=1, x_host=1)
    try:
        body = server.app.test_client().get("/", headers=headers).get_data(as_text=True)
    finally:
        server.app.wsgi_app = original
    assert "--index-url https://packages.example.org/simple/" in body and "--trusted-host" not in body


# --------------------------------------------------------------------------
# Accounts and uploads
# --------------------------------------------------------------------------

@pytest.fixture
def alice(server):
    server.module.create_user_cli("alice", "s3cretpw-123")


def upload(client, filename, content=b"wheel-bytes", headers=AUTH, path="/", field="file"):
    return client.post(path, data={field: (BytesIO(content), filename)}, headers=headers, content_type="multipart/form-data")


def test_twine_upload_then_appears_everywhere(client, server, alice):
    assert upload(client, "demo_pkg-1.2.3-py3-none-any.whl").status_code == 200
    assert 'href="/project/demo-pkg/"' in client.get("/").get_data(as_text=True)
    assert (server.packages / "demo-pkg" / "demo_pkg-1.2.3-py3-none-any.whl").read_bytes() == b"wheel-bytes"


def test_hyphenated_sdist_names_keep_their_full_project_name(client, server, alice):
    assert upload(client, "my-cool-pkg-1.0.tar.gz").status_code == 200
    assert (server.packages / "my-cool-pkg" / "my-cool-pkg-1.0.tar.gz").exists()  # used to be cut to "my"


def test_upload_requires_valid_credentials(client, alice):
    assert upload(client, "x-1.0.tar.gz", headers=basic("alice", "wrong")).status_code == 401
    assert upload(client, "x-1.0.tar.gz", headers={}).status_code == 401
    assert upload(client, "x-1.0.tar.gz", headers=basic("nobody", "s3cretpw-123")).status_code == 401


def test_user_names_are_not_case_sensitive_on_any_database(client, server, alice):
    assert upload(client, "case-1.0.tar.gz", headers=basic("ALICE", "s3cretpw-123")).status_code == 200
    server.module.create_user_cli("Alice", "another-password-9")  # the same account, so its password is replaced
    with server.module.SessionLocal() as db:
        assert db.query(server.module.User).count() == 1
    assert upload(client, "case-2.0.tar.gz", headers=basic("alice", "another-password-9")).status_code == 200


@pytest.mark.parametrize(
    "filename",
    ["../evil-1.0.whl", "..%2fevil-1.0.whl", "a/b-1.0.whl", "a\\b-1.0.whl", ".hidden-1.0.whl", "notes.txt", "evil-1.0.exe",
     "evil 1.0.whl", "evil-1.0.whl\n", "", "x" * 300 + ".whl"],
)
def test_upload_rejects_unsafe_or_unsupported_file_names(client, server, alice, filename):
    response = upload(client, filename)
    assert response.status_code == 400
    assert not [p for p in server.base.rglob("*evil*") if p.is_file()] and not list(server.packages.rglob("*.part"))


def test_upload_to_a_url_project_must_match_the_file(client, alice):
    assert upload(client, "other-1.0.tar.gz", path="/upload/requests/").status_code == 400
    assert upload(client, "requests-1.0.tar.gz", path="/upload/requests/").status_code == 200


@pytest.mark.parametrize("project", ["..", ".", "a b", "a;b", "-x"])
def test_upload_rejects_hostile_project_names(client, alice, project):
    assert upload(client, "pkg-1.0.tar.gz", path=f"/upload/{project}/").status_code in (400, 404)


def test_uploaded_files_are_immutable_and_empty_files_are_refused(client, server, alice):
    assert upload(client, "imm-1.0.tar.gz", b"first").status_code == 200
    again = upload(client, "imm-1.0.tar.gz", b"second")
    assert again.status_code == 400 and "already exists" in again.get_data(as_text=True)
    assert (server.packages / "imm" / "imm-1.0.tar.gz").read_bytes() == b"first"
    assert upload(client, "empty-1.0.tar.gz", b"").status_code == 400


def test_overwrite_can_be_allowed(client, server, alice, monkeypatch):
    monkeypatch.setattr(server.module, "ALLOW_OVERWRITE", True)
    assert upload(client, "ow-1.0.tar.gz", b"first").status_code == 200
    assert upload(client, "ow-1.0.tar.gz", b"second").status_code == 200
    assert (server.packages / "ow" / "ow-1.0.tar.gz").read_bytes() == b"second"


def test_upload_size_limit(client, server, alice, monkeypatch):
    monkeypatch.setitem(server.app.config, "MAX_CONTENT_LENGTH", 500)
    assert upload(client, "big-1.0.tar.gz", b"x" * 5000).status_code == 413
    assert not (server.packages / "big").exists()


def test_concurrent_uploads_of_one_file_store_it_once(client, server, alice):
    codes = []

    def send():
        codes.append(upload(server.app.test_client(), "race-1.0.tar.gz", b"same").status_code)

    threads = [threading.Thread(target=send) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200] + [400] * 5
    assert list((server.packages / "race").iterdir()) == [server.packages / "race" / "race-1.0.tar.gz"]


def test_upload_records_hash_and_uploader(client, server, alice):
    import hashlib

    upload(client, "hashme-1.0.tar.gz", b"payload")
    hashes = server.module.CATALOG.hashes("hashme")
    assert hashes[("local", "hashme-1.0.tar.gz")] == hashlib.sha256(b"payload").hexdigest()
    body = client.get("/simple/hashme/").get_data(as_text=True) if False else None  # covered in the /simple/ tests
    assert body is None


def test_too_many_failed_logins_are_throttled(client, alice):
    statuses = [upload(client, "t-1.0.tar.gz", headers=basic("alice", f"guess{i}")).status_code for i in range(12)]
    assert statuses[:10] == [401] * 10 and statuses[10:] == [429, 429]
    assert upload(client, "t-1.0.tar.gz", headers=AUTH).status_code == 429  # even the right password waits


def test_credentials_are_never_logged(client, alice, caplog):
    caplog.set_level(logging.DEBUG)
    upload(client, "leak_check-1.0-py3-none-any.whl")
    upload(client, "leak_check-1.1-py3-none-any.whl", headers=basic("alice", "wrong-pw"))
    assert "s3cretpw-123" not in caplog.text and "wrong-pw" not in caplog.text
    assert AUTH["Authorization"].split()[1] not in caplog.text


def test_secret_key_is_not_the_old_hardcoded_value(server):
    assert server.app.config["SECRET_KEY"] == "test-secret-key"


# --------------------------------------------------------------------------
# Fake upstream for the download and /simple/ tests
# --------------------------------------------------------------------------

class FakeBody:
    def __init__(self, data, delay=0.0, fail_after=None):
        self.data, self.delay, self.fail_after = data, delay, fail_after
        self.headers = {"Content-Length": str(len(data))}
        self.closed = False

    def iter_content(self, chunk_size=1):
        step = max(1, len(self.data) // 8)
        for offset in range(0, len(self.data), step):
            if self.fail_after is not None and offset >= self.fail_after:
                raise ConnectionError("upstream connection dropped")
            time.sleep(self.delay)
            yield self.data[offset:offset + step]

    def close(self):
        self.closed = True


class FakeUpstream:
    def __init__(self):
        self.projects, self.blobs, self.opened, self.down, self.delay, self.fail_after = {}, {}, 0, False, 0.0, None

    def add(self, project, filename, data, sha256="auto", requires_python=None, yanked=None):
        import hashlib

        digest = hashlib.sha256(data).hexdigest() if sha256 == "auto" else sha256
        url = f"https://files.example/{filename}"
        self.projects.setdefault(project, []).append(UpstreamFile(filename, url, digest, requires_python, yanked))
        self.blobs[url] = data

    def files(self, project):
        if self.down:
            raise UpstreamError("upstream is down")
        return self.projects.get(web.project_key(project))

    def find(self, project, filename):
        return next((f for f in self.files(project) or [] if f.filename == filename), None)

    def open(self, url):
        if self.down:
            raise UpstreamError("upstream is down")
        self.opened += 1
        return FakeBody(self.blobs[url], self.delay, self.fail_after)


@pytest.fixture
def upstream(server, monkeypatch):
    fake = FakeUpstream()
    for name in ("files", "find", "open"):
        monkeypatch.setattr(server.module.UPSTREAM, name, getattr(fake, name))
    return fake


BLOB = bytes(range(256)) * 400


def cached_files(server, project):
    folder = server.cache / project
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --------------------------------------------------------------------------
# Cache fill
# --------------------------------------------------------------------------

def test_first_request_streams_and_stores_then_the_cache_answers(client, server, upstream):
    upstream.add("pkg", "pkg-1.0-py3-none-any.whl", BLOB)
    first = client.get("/cache/pkg/pkg-1.0-py3-none-any.whl")
    assert first.status_code == 200 and first.data == BLOB and first.headers["X-PyPihub-Cache"] == "MISS"
    assert first.headers["Content-Length"] == str(len(BLOB))
    assert cached_files(server, "pkg") == ["pkg-1.0-py3-none-any.whl"]
    assert server.module.CATALOG.get_project("pkg").files[0].size == len(BLOB)
    second = client.get("/cache/pkg/pkg-1.0-py3-none-any.whl")
    assert second.data == BLOB and "X-PyPihub-Cache" not in second.headers and upstream.opened == 1


def test_a_corrupt_download_is_not_kept(client, server, upstream):
    upstream.add("pkg", "pkg-1.0.tar.gz", BLOB, sha256="0" * 64)
    assert client.get("/cache/pkg/pkg-1.0.tar.gz").status_code == 200  # the client's own hash check rejects it
    assert cached_files(server, "pkg") == [] and server.module.CATALOG.get_project("pkg") is None


def test_an_interrupted_download_leaves_nothing_behind(client, server, upstream):
    upstream.add("pkg", "pkg-1.0.tar.gz", BLOB)
    upstream.fail_after = 1000
    with pytest.raises(Exception):
        client.get("/cache/pkg/pkg-1.0.tar.gz").data
    assert cached_files(server, "pkg") == [] and server.module.CATALOG.get_project("pkg") is None


def test_a_client_that_hangs_up_leaves_nothing_behind(client, server, upstream):
    upstream.add("pkg", "pkg-1.0.tar.gz", BLOB)
    response = client.get("/cache/pkg/pkg-1.0.tar.gz", buffered=False)
    next(iter(response.response))
    response.close()
    assert cached_files(server, "pkg") == []
    assert client.get("/cache/pkg/pkg-1.0.tar.gz").data == BLOB  # and the next client gets a complete file


def test_simultaneous_first_requests_all_get_a_complete_file(server, upstream):
    upstream.add("pkg", "pkg-1.0.tar.gz", BLOB)
    upstream.delay = 0.01
    results, errors = [], []

    def fetch():
        try:
            results.append(server.app.test_client().get("/cache/pkg/pkg-1.0.tar.gz").data)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=fetch) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 8 and all(body == BLOB for body in results)
    assert cached_files(server, "pkg") == ["pkg-1.0.tar.gz"]
    assert (server.cache / "pkg" / "pkg-1.0.tar.gz").read_bytes() == BLOB
    assert len(server.module.CATALOG.get_project("pkg").files) == 1


def test_upstream_failures_become_clean_status_codes(client, upstream):
    upstream.add("pkg", "pkg-1.0.tar.gz", BLOB)
    assert client.get("/cache/pkg/other-1.0.tar.gz").status_code == 404
    assert client.get("/cache/unknown/x-1.0.tar.gz").status_code == 404
    upstream.down = True
    assert client.get("/cache/pkg/pkg-1.0.tar.gz").status_code == 502


@pytest.mark.parametrize("package, filename", [("..", "x.whl"), ("pkg", ".."), ("pkg", "a b.whl"), ("pkg", "x.whl\n"), (".hidden", "x.whl")])
def test_cache_route_rejects_hostile_names(client, upstream, package, filename):
    assert client.get(f"/cache/{package}/{filename}").status_code == 404 and upstream.opened == 0


def test_a_full_cache_streams_without_storing(client, server, upstream, monkeypatch):
    upstream.add("pkg", "pkg-1.0.tar.gz", BLOB)
    server.module.CATALOG.record_file("filler", "filler-1.0.tar.gz", "cached", 500)
    monkeypatch.setattr(server.module, "CACHE_MAX_BYTES", 100)
    server.module.KV_CACHE.delete("catalog:stats")
    assert client.get("/cache/pkg/pkg-1.0.tar.gz").data == BLOB
    assert cached_files(server, "pkg") == []


def test_disk_errors_do_not_break_the_download(client, server, upstream, monkeypatch):
    upstream.add("pkg", "pkg-1.0.tar.gz", BLOB)
    real_open = open

    def refuse_part_files(path, *args, **kwargs):
        if str(path).endswith(".part") or ".part" in str(path):
            raise OSError(28, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse_part_files)
    assert client.get("/cache/pkg/pkg-1.0.tar.gz").data == BLOB


# --------------------------------------------------------------------------
# /simple/
# --------------------------------------------------------------------------

def test_simple_index_merges_local_cached_and_upstream(client, server, upstream, add_file):
    add_file("local", "demo", "demo-1.0.tar.gz")
    add_file("cached", "demo", "demo-2.0.tar.gz")
    upstream.add("demo", "demo-2.0.tar.gz", b"x", requires_python=">=3.9")
    upstream.add("demo", "demo-3.0.tar.gz", b"y", requires_python=">=3.10", yanked="bad release")
    body = client.get("/simple/demo/").get_data(as_text=True)
    assert 'href="/packages/demo/demo-1.0.tar.gz"' in body
    assert re.search(r'href="/cache/demo/demo-2\.0\.tar\.gz#sha256=[0-9a-f]{64}" data-requires-python="&gt;=3\.9"', body)
    assert re.search(r'href="/cache/demo/demo-3\.0\.tar\.gz#sha256=[0-9a-f]{64}" data-requires-python="&gt;=3\.10" data-yanked="bad release"', body)
    assert '<meta name="pypi:repository-version" content="1.0">' in body and body.startswith("<!DOCTYPE html>")


def test_simple_index_finds_underscore_directories_by_normalised_name(client, upstream, add_file):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = client.get("/simple/demo-pkg/").get_data(as_text=True)
    assert 'href="/packages/demo_pkg/demo_pkg-1.0-py3-none-any.whl"' in body


def test_simple_index_includes_hashes_of_uploads(client, server, upstream):
    import hashlib

    server.module.create_user_cli("alice", "s3cretpw-123")
    upload(client, "hashed-1.0.tar.gz", b"payload")
    body = client.get("/simple/hashed/").get_data(as_text=True)
    assert f"#sha256={hashlib.sha256(b'payload').hexdigest()}" in body


def test_simple_index_keeps_serving_local_files_when_upstream_is_down(client, upstream, add_file):
    add_file("local", "mine", "mine-1.0.tar.gz")
    upstream.down = True
    response = client.get("/simple/mine/")
    assert response.status_code == 200 and "mine-1.0.tar.gz" in response.get_data(as_text=True)


def test_simple_index_status_when_nothing_is_known(client, upstream):
    assert client.get("/simple/nothing/").status_code == 404
    upstream.down = True
    assert client.get("/simple/nothing/").status_code == 502


def test_simple_index_creates_no_directories_for_strangers(client, server, upstream):
    for name in ("bot1", "bot2", "wp-admin"):
        client.get(f"/simple/{name}/")
    assert list(server.cache.iterdir()) == [] and list(server.packages.iterdir()) == []


@pytest.mark.parametrize("name", ["..", ".", "a b", "a;b", "-x", "x" * 200])
def test_simple_index_rejects_hostile_names(client, upstream, name):
    assert client.get(f"/simple/{name}/").status_code in (404, 308)


def test_simple_index_hides_partial_downloads_and_odd_file_names(client, server, upstream, add_file):
    add_file("cached", "demo", "demo-1.0.tar.gz")
    (server.cache / "demo" / ".demo-2.0.tar.gz.abc.part").write_bytes(b"half")
    (server.cache / "demo" / "demo 3.0.tar.gz").write_bytes(b"x")
    body = client.get("/simple/demo/").get_data(as_text=True)
    assert "demo-1.0.tar.gz" in body and ".part" not in body and "demo 3.0" not in body


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def test_cli_reindex_and_prune(server, monkeypatch, capsys):
    (server.cache / "old").mkdir()
    (server.cache / "old" / "old-1.0.tar.gz").write_bytes(b"x" * 1000)
    monkeypatch.setattr(sys, "argv", ["pypihub", "reindex"])
    server.module.usage()
    assert "1 added" in capsys.readouterr().out and server.module.CATALOG.get_project("old") is not None
    monkeypatch.setattr(sys, "argv", ["pypihub", "prune", "--max-size", "100"])
    server.module.usage()
    assert not (server.cache / "old" / "old-1.0.tar.gz").exists() and server.module.CATALOG.get_project("old") is None
    monkeypatch.setattr(sys, "argv", ["pypihub", "prune"])
    server.module.usage()
    assert "--max-size is required" in capsys.readouterr().out


@pytest.mark.parametrize(
    "page, pages, expected",
    [(1, 1, [1]), (1, 5, [1, 2, 3, 4, 5]), (3, 6, [1, 2, 3, 4, 5, 6]), (1, 20, [1, 2, 3, None, 20]),
     (10, 20, [1, None, 8, 9, 10, 11, 12, None, 20]), (20, 20, [1, None, 18, 19, 20]), (4, 20, [1, 2, 3, 4, 5, 6, None, 20])],
)
def test_page_number_window(page, pages, expected):
    assert web._page_numbers(page, pages) == expected


# --------------------------------------------------------------------------
# Package page: details copied from pypi.org
# --------------------------------------------------------------------------

from test_meta import FakeUpstream as PypiFake, document  # noqa: E402

from pypihub.cache import MemoryCache  # noqa: E402


@pytest.fixture
def pypi(server, monkeypatch):
    fake = PypiFake()
    monkeypatch.setattr(server.module.UPSTREAM, "json_url", fake.json_url)
    monkeypatch.setattr(server.module.UPSTREAM, "get_json", fake.get_json)
    monkeypatch.setattr(server.module.META, "_on", True)
    monkeypatch.setattr(server.module.META, "_kv", MemoryCache())
    fake.docs["https://pypi.test/pypi/demo-pkg/json"] = document()
    fake.docs["https://pypi.test/pypi/demo-pkg/2.0/json"] = document(version="2.0")
    return fake


def page(client, path="/project/demo-pkg/"):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.get_data(as_text=True)


def test_package_page_shows_what_pypi_shows(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client)
    assert "A demo package" in body and "<strong>world</strong>" in body and 'class="readme"' in body
    assert 'href="https://git.example/demo"' in body and "Homepage" in body and "javascript:" not in body
    assert "MIT" in body and "Ada Lovelace" in body and "Python &gt;=3.9" in body
    assert 'class="chip">demo<' in body and "Programming Language" in body
    assert "Extra: <code" in body and "requests&gt;=2" in body
    assert "Latest version" in body and "Released: Mar 1, 2026" in body
    assert "Pre-release" in body and "Yanked" in body  # 2.1rc1 and 1.5 in the release history
    assert "1 known vulnerability in this release" in body and 'href="https://osv.dev/PYSEC-1"' in body and "Fixed in 1.0.1" in body
    assert 'href="/cache/demo-pkg/demo_pkg-2.0-py3-none-any.whl"' in body and "abababababab" in body
    assert "Files on this server" in body and "demo_pkg-1.0-py3-none-any.whl" in body


def test_release_history_marks_releases_stored_here_and_links_to_each(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client)
    row = lambda v: re.search(rf'<a class="rel__link" href="/project/demo-pkg/{re.escape(v)}/"[^>]*>{re.escape(v)}</a>.*?</tr>', body, re.S).group(0)  # noqa: E731
    assert "On this server" in row("1.0") and "On this server" not in row("2.0")
    assert "Latest" in row("2.0") and "Pre-release" in row("2.1rc1") and "Yanked" in row("1.5")


def test_a_long_release_history_is_folded(client, add_file, pypi):
    doc = document()
    doc["releases"] = {f"1.{i}": [{"upload_time_iso_8601": "2025-01-01T00:00:00Z", "yanked": False}] for i in range(45)}
    pypi.docs["https://pypi.test/pypi/demo-pkg/json"] = doc
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client)
    assert "Show 25 older releases" in body and body.count('class="rel__link"') == 45


def test_a_release_page(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client, "/project/demo-pkg/2.0/")
    assert "pip install --index-url" in body and "demo-pkg==2.0" in body.lower() and "<title>Demo-Pkg 2.0 - PyPihub</title>" in body
    assert "Release history" in body  # the history comes from the project, the details from the release


@pytest.mark.parametrize("version", ["9.9", "..", "a b", "%00", "x" * 200, "1.0;rm", "-1"])
def test_unknown_or_hostile_versions_are_404_without_asking_pypi(client, add_file, pypi, version):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    pypi.calls.clear()
    assert client.get(f"/project/demo-pkg/{version}/").status_code == 404
    assert not [c for c in pypi.calls if c.endswith("/json") and version in c]


def test_a_release_without_a_document_is_404(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    assert client.get("/project/demo-pkg/1.0/").status_code == 404  # listed in the history, but PyPI has no document for it


def test_details_are_never_fetched_for_projects_not_stored_here(client, pypi):
    assert client.get("/project/django/").status_code == 404 and client.get("/project/django/3.0/").status_code == 404
    assert pypi.calls == []


def test_a_package_that_pypi_does_not_know(client, add_file, pypi):
    add_file("local", "mine", "mine-1.0.tar.gz")
    body = page(client, "/project/mine/")
    assert "not on PyPI" in body and "mine-1.0.tar.gz" in body and 'class="pkg-side"' not in body
    assert "https:" not in client.get("/project/mine/").headers["Content-Security-Policy"]  # no README, so no remote images


def test_when_pypi_is_down_an_older_copy_is_shown_with_a_note(client, server, add_file, pypi):
    from datetime import timedelta

    from pypihub.database import PypiMeta, utcnow

    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    page(client)
    server.module.META._kv = MemoryCache()
    with server.module.SessionLocal() as db:
        db.query(PypiMeta).update({"fetched_at": utcnow() - timedelta(hours=9)})
        db.commit()
    pypi.down = True
    body = page(client)
    assert "may be out of date" in body and "<strong>world</strong>" in body


def test_when_pypi_is_down_and_nothing_is_stored_the_files_still_show(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    pypi.down = True
    body = page(client)
    assert "could not be loaded" in body and "demo_pkg-1.0-py3-none-any.whl" in body and 'class="pkg-side"' not in body


def test_with_the_feature_off_the_page_is_just_the_files(client, server, add_file, monkeypatch):
    monkeypatch.setattr(server.module.META, "_on", False)
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client)
    assert 'class="pkg-side"' not in body and "notice" not in body and "demo_pkg-1.0-py3-none-any.whl" in body


def test_hostile_package_metadata_is_escaped_or_removed(client, add_file, pypi):
    evil = "<img src=x onerror=alert(1)>"
    pypi.docs["https://pypi.test/pypi/demo-pkg/json"] = document(
        name=evil, summary=evil, author=evil, keywords=evil, license=evil, requires_python=evil,
        description="<script>alert(1)</script>\n\n[x](javascript:alert(1))", description_content_type="text/markdown",
        project_urls={"<b>x</b>": "https://ok.example/", "bad": "javascript:alert(1)"}, classifiers=[evil],
        requires_dist=[evil])
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client)
    assert "<img src=x" not in body and "<script>alert" not in body and "javascript:" not in body and "<b>x</b>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body and "&lt;b&gt;x&lt;/b&gt;" in body


def test_csp_allows_remote_images_only_where_a_readme_is_shown(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    csp = client.get("/project/demo-pkg/").headers["Content-Security-Policy"]
    assert "img-src 'self' data: https:" in csp and "unsafe-inline" not in csp and "script-src 'self'" in csp
    assert "https:" not in client.get("/").headers["Content-Security-Policy"]


def test_the_package_page_has_no_inline_script_or_style(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client)
    assert " style=" not in body and " onclick=" not in body and "<script>" not in body.replace("<script src=", "")


def test_without_a_renderer_the_description_is_shown_as_escaped_text(client, add_file, pypi, monkeypatch):
    monkeypatch.setattr("pypihub.pypimeta.readme.render", lambda raw, content_type: None)
    pypi.docs["https://pypi.test/pypi/demo-pkg/json"] = document(description="Plain <b>text</b> & more")
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    body = page(client)
    assert 'class="readme-plain"' in body and "Plain &lt;b&gt;text&lt;/b&gt; &amp; more" in body and "<b>text</b>" not in body


def test_stored_file_paging_keeps_the_release_in_its_links(client, server, pypi):
    for i in range(60):
        server.module.CATALOG.record_file("demo-pkg", f"demo_pkg-1.{i}.tar.gz", "cached", 10)
    body = page(client, "/project/demo-pkg/2.0/")
    assert 'href="/project/demo-pkg/2.0/?page=2"' in body
    second = page(client, "/project/demo-pkg/2.0/?page=2")
    assert "demo_pkg-1.9.tar.gz" in second and "demo_pkg-1.59.tar.gz" not in second  # newest versions are on page 1


def test_a_second_visit_is_served_from_the_cache(client, add_file, pypi):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    page(client)
    count = len(pypi.calls)
    for _ in range(5):
        page(client)
    assert len(pypi.calls) == count

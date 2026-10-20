#!/usr/bin/env python3

# File: tests/test_web.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-21
# Description: 
# License: MIT

import base64
import logging
import time
from unittest import mock

import pytest

from pypihub import web


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename, kind, version",
    [
        ("requests-2.32.3-py3-none-any.whl", "wheel", "2.32.3"),
        ("torch-2.0.0+cu118-cp311-cp311-linux_x86_64.whl", "wheel", "2.0.0+cu118"),
        ("my_pkg-1.0.tar.gz", "sdist", "1.0"),
        ("old-0.1.zip", "sdist", "0.1"),
        ("thing.egg", "other", None),
        ("notes.txt", "other", None),
        ("broken-version-x.y.whl", "other", None),
    ],
)
def test_parse_dist_filename(filename, kind, version):
    got_kind, got_version = web.parse_dist_filename(filename)
    assert got_kind == kind
    assert (str(got_version) if got_version else None) == version


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
# Index page
# --------------------------------------------------------------------------

def test_index_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Nothing is stored here yet" in body
    assert "data-pkg-table" not in body


def test_index_lists_local_and_cached(client, add_file):
    add_file("local", "mylib", "mylib-0.4.1-py3-none-any.whl", b"x" * 2000)
    add_file("local", "mylib", "mylib-0.3.0.tar.gz")
    add_file("cached", "requests", "requests-2.32.3-py3-none-any.whl", b"y" * 5000)
    body = client.get("/").get_data(as_text=True)
    assert 'href="/project/mylib/"' in body and 'href="/project/requests/"' in body
    assert "0.4.1" in body and "2.32.3" in body
    assert 'data-source="local"' in body and 'data-source="cached"' in body
    assert "2 packages, 3 files" in body


def test_latest_version_ignores_prereleases(add_file, server):
    add_file("local", "pre", "pre-1.0-py3-none-any.whl")
    add_file("local", "pre", "pre-2.0rc1-py3-none-any.whl")
    package = web.scan(server.packages, server.cache).by_key["pre"]
    assert package.latest == "1.0"


def test_empty_directories_are_ignored(client, server):
    (server.cache / "ghost").mkdir()  # pip's index lookups create these for unknown names
    assert "Nothing is stored here yet" in client.get("/").get_data(as_text=True)


def test_same_directory_is_not_counted_twice(server, add_file):
    add_file("local", "solo", "solo-1.0.tar.gz")
    snapshot = web.scan(server.packages, server.packages)
    assert [len(p.files) for p in snapshot.packages] == [1]
    assert snapshot.packages[0].sources == ["local"]


def test_names_differing_only_in_spelling_are_merged(server, add_file):
    add_file("local", "my_pkg", "my_pkg-1.0.tar.gz")
    add_file("cached", "my-pkg", "my_pkg-2.0.tar.gz")
    snapshot = web.scan(server.packages, server.cache)
    assert len(snapshot.packages) == 1
    assert snapshot.packages[0].key == "my-pkg"
    assert snapshot.packages[0].sources == ["local", "cached"]


def test_file_names_are_escaped(client, add_file):
    add_file("local", "xss", "xss-1.0-py3-none-any.whl")
    add_file("local", "xss", "<img src=x onerror=alert(1)>.txt")
    body = client.get("/project/xss/").get_data(as_text=True)
    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;.txt" in body


def test_large_index_renders_quickly(client, server):
    for i in range(1500):
        folder = server.cache / f"pkg{i:04d}"
        folder.mkdir()
        (folder / f"pkg{i:04d}-1.0.tar.gz").write_bytes(b"x")
    server.module.web_ui.invalidate_cache()
    start = time.perf_counter()
    response = client.get("/")
    assert response.status_code == 200
    assert time.perf_counter() - start < 3
    assert "1,500 packages" in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# Package page
# --------------------------------------------------------------------------

def test_package_page_lists_files_with_download_links(client, add_file):
    add_file("local", "mylib", "mylib-0.4.1-py3-none-any.whl")
    add_file("cached", "mylib", "mylib-0.3.0.tar.gz")
    response = client.get("/project/mylib/")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "/packages/mylib/mylib-0.4.1-py3-none-any.whl" in body
    assert "/cache/mylib/mylib-0.3.0.tar.gz" in body
    assert body.index("mylib-0.4.1") < body.index("mylib-0.3.0")  # newest first
    assert "pip install --index-url" in body


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
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_pages_have_no_inline_script_or_style(client, add_file):
    add_file("local", "mylib", "mylib-1.0.tar.gz")
    for path in ("/", "/project/mylib/", "/project/none/"):
        body = client.get(path, headers={"Accept": "text/html"}).get_data(as_text=True)
        assert " style=" not in body, path
        assert " onclick=" not in body and "<script>" not in body, path


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok" and data["checks"]["packages_dir_writable"] and data["version"]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "path, mimetype",
    [
        ("css/pypihub.css", "text/css"),
        ("js/pypihub.js", "text/javascript"),
        ("js/theme.js", "text/javascript"),
        ("vendor/bootstrap/bootstrap.min.css", "text/css"),
        ("vendor/bootstrap/bootstrap.bundle.min.js", "text/javascript"),
        ("fonts/bricolage-grotesque-latin-wght-normal.woff2", "font/woff2"),
        ("fonts/jetbrains-mono-latin-wght-normal.woff2", "font/woff2"),
        ("img/logo-72.png", "image/png"),
        ("img/favicon-32.png", "image/png"),
    ],
)
def test_static_assets_are_served(client, path, mimetype):
    response = client.get(f"/static/{path}")
    assert response.status_code == 200
    assert response.mimetype in (mimetype, "application/javascript")
    assert len(response.data) > 100


def test_every_asset_referenced_by_a_page_exists(client):
    import re

    body = client.get("/").get_data(as_text=True)
    urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', body)
    assert len(urls) >= 6
    for url in urls:
        assert client.get(url).status_code == 200, url


# --------------------------------------------------------------------------
# pip command shown on the page
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "base_url, expect_trusted",
    [
        ("http://192.168.1.10:5000", "--trusted-host 192.168.1.10"),
        ("http://pypi.lan:8080", "--trusted-host pypi.lan"),
        ("http://localhost:5000", None),
        ("http://127.0.0.1:5000", None),
        ("https://pypi.example.org", None),
    ],
)
def test_pip_command_trusted_host(client, base_url, expect_trusted):
    body = client.get("/", base_url=base_url).get_data(as_text=True)
    assert f"--index-url {base_url}/simple/" in body
    assert ("--trusted-host" in body) == bool(expect_trusted)
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
    assert "--index-url https://packages.example.org/simple/" in body
    assert "--trusted-host" not in body


# --------------------------------------------------------------------------
# Existing behaviour must keep working
# --------------------------------------------------------------------------

AUTH = {"Authorization": "Basic " + base64.b64encode(b"alice:s3cretpw-123").decode()}


@pytest.fixture
def alice(server):
    server.module.create_user_cli("alice", "s3cretpw-123")


def _upload(client, filename, headers=AUTH):
    from io import BytesIO

    return client.post("/", data={"file": (BytesIO(b"wheel-bytes"), filename)}, headers=headers,
                       content_type="multipart/form-data")


def test_twine_upload_then_appears_in_ui(client, alice):
    assert _upload(client, "demo_pkg-1.2.3-py3-none-any.whl").status_code == 200
    body = client.get("/").get_data(as_text=True)  # no manual cache invalidation: uploads do it
    assert 'href="/project/demo-pkg/"' in body and "1.2.3" in body


def test_upload_requires_valid_credentials(client, alice):
    bad = {"Authorization": "Basic " + base64.b64encode(b"alice:wrong").decode()}
    assert _upload(client, "x-1.0.tar.gz", headers=bad).status_code == 401
    assert _upload(client, "x-1.0.tar.gz", headers={}).status_code == 401


def test_credentials_are_never_logged(client, alice, caplog):
    caplog.set_level(logging.DEBUG)
    _upload(client, "leak_check-1.0-py3-none-any.whl")
    _upload(client, "leak_check-1.1-py3-none-any.whl", headers={"Authorization": "Basic " + base64.b64encode(b"alice:wrong-pw").decode()})
    logged = caplog.text
    assert "s3cretpw-123" not in logged and "wrong-pw" not in logged
    assert AUTH["Authorization"].split()[1] not in logged


def test_secret_key_is_not_the_old_hardcoded_value(server):
    assert server.app.config["SECRET_KEY"] == "test-secret-key"


def test_simple_index_still_serves_local_files(client, server, add_file):
    add_file("local", "mylib", "mylib-1.0.tar.gz")
    fake = mock.Mock(status_code=404, text="")
    with mock.patch.object(server.module.requests, "get", return_value=fake):
        body = client.get("/simple/mylib/").get_data(as_text=True)
    assert 'href="/packages/mylib/mylib-1.0.tar.gz"' in body


# --------------------------------------------------------------------------
# PEP 503: pip asks for "my-pkg", twine stores "my_pkg"
# --------------------------------------------------------------------------

def _simple(client, server, name):
    fake = mock.Mock(status_code=404, text="")
    with mock.patch.object(server.module.requests, "get", return_value=fake):
        return client.get(f"/simple/{name}/")


def test_simple_index_finds_underscore_directory_by_normalised_name(client, server, add_file):
    add_file("local", "demo_pkg", "demo_pkg-1.0-py3-none-any.whl")
    response = _simple(client, server, "demo-pkg")  # what pip requests
    assert response.status_code == 200
    assert 'href="/packages/demo_pkg/demo_pkg-1.0-py3-none-any.whl"' in response.get_data(as_text=True)
    assert client.get("/packages/demo_pkg/demo_pkg-1.0-py3-none-any.whl").status_code == 200


def test_simple_index_cached_underscore_directory(client, server, add_file):
    add_file("cached", "Foo.Bar", "foo.bar-2.0.tar.gz")
    body = _simple(client, server, "foo-bar").get_data(as_text=True)
    assert 'href="/cache/Foo.Bar/foo.bar-2.0.tar.gz"' in body


def test_simple_index_unknown_package_is_404(client, server):
    assert _simple(client, server, "nothing-here").status_code == 404


@pytest.mark.parametrize("name", [".", ".."])
def test_simple_index_rejects_dot_names(client, server, name):
    assert _simple(client, server, name).status_code == 404

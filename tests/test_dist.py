import pytest

from pypihub import dist


@pytest.mark.parametrize("name", ["requests", "my_pkg", "Flask-Cors", "a", "zope.interface", "x" * 128])
def test_valid_project_names(name):
    assert dist.valid_project_name(name)


@pytest.mark.parametrize(
    "name", ["", ".", "..", "-x", "x-", ".hidden", "a/b", "a\\b", "a b", "a\x00b", "x" * 129, "é", None, 5, "a;b", "a%2fb"]
)
def test_invalid_project_names(name):
    assert not dist.valid_project_name(name)


@pytest.mark.parametrize("name", ["a-1.0.whl", "torch-2.0.0+cu118-cp311-linux_x86_64.whl", "pkg-1.0.tar.gz", "A.B-1_c.zip"])
def test_valid_filenames(name):
    assert dist.valid_filename(name) and dist.is_upload_filename(name)


@pytest.mark.parametrize(
    "name", ["", "..", "../x.whl", "a/b.whl", "a\\b.whl", ".x.whl", "a b.whl", "x.whl\n", "x\x00.whl", "a" * 256, "-x.whl", None, "x.whl;"]
)
def test_invalid_filenames(name):
    assert not dist.valid_filename(name)


def test_upload_suffixes_are_limited_but_downloads_accept_legacy_names():
    assert dist.valid_filename("old-1.0.egg") and not dist.is_upload_filename("old-1.0.egg")
    assert not dist.is_upload_filename("notes.txt") and not dist.is_upload_filename("run.exe")


@pytest.mark.parametrize(
    "filename, kind, key, version",
    [
        ("requests-2.32.3-py3-none-any.whl", "wheel", "requests", "2.32.3"),
        ("My_Pkg-1.0-py3-none-any.whl", "wheel", "my-pkg", "1.0"),
        ("torch-2.0.0+cu118-cp311-cp311-linux_x86_64.whl", "wheel", "torch", "2.0.0+cu118"),
        ("my-pkg-1.0.tar.gz", "sdist", "my-pkg", "1.0"),
        ("old-0.1.zip", "sdist", "old", "0.1"),
        ("thing-1.0.egg", "other", None, None),
        ("broken-version-x.y.whl", "other", None, None),
    ],
)
def test_parse_dist_filename(filename, kind, key, version):
    got_kind, got_key, got_version = dist.parse_dist_filename(filename)
    assert (got_kind, got_key, str(got_version) if got_version else None) == (kind, key, version)


def test_project_key_matches_pep_503():
    assert dist.project_key("Flask_Cors.X") == "flask-cors-x"


@pytest.mark.parametrize("text, value", [("20GB", 20 * 1024**3), ("512 MiB", 512 * 1024**2), ("1.5g", int(1.5 * 1024**3)),
                                         ("100", 100), ("2k", 2048), ("1TB", 1024**4)])
def test_parse_size(text, value):
    assert dist.parse_size(text) == value


@pytest.mark.parametrize("text", ["", "abc", "-5GB", "5 XB", "GB"])
def test_parse_size_rejects_nonsense(text):
    with pytest.raises(ValueError):
        dist.parse_size(text)

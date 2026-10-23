#!/usr/bin/env python3

# File: pypihub/dist.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Pure helpers for project names and distribution file names (no Flask, no database)
# License: LGPL-3.0-or-later

"""
Validation and parsing of project and file names.

Names come from URLs, uploads and upstream indexes, so they are checked before they are used to
build a path on disk. Nothing here touches the file system.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

# What an upload may be. Downloads accept any safe name, because an upstream index can list legacy formats.
UPLOAD_SUFFIXES = (".whl", ".tar.gz", ".zip")

MAX_PROJECT_LENGTH = 128
MAX_FILENAME_LENGTH = 255

_PROJECT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b)?\s*$", re.I)


def project_key(name: str) -> str:
    """PEP 503 normalised name: lower case, runs of ``-``, ``_`` and ``.`` become ``-``."""
    return canonicalize_name(name)


def valid_project_name(name: object) -> bool:
    return isinstance(name, str) and len(name) <= MAX_PROJECT_LENGTH and bool(_PROJECT_RE.fullmatch(name))


def valid_filename(name: object) -> bool:
    """A single path component made of characters that real distribution file names use."""
    return isinstance(name, str) and len(name) <= MAX_FILENAME_LENGTH and bool(_FILENAME_RE.fullmatch(name))


def is_upload_filename(name: object) -> bool:
    return valid_filename(name) and str(name).endswith(UPLOAD_SUFFIXES)


def parse_dist_filename(filename: str) -> Tuple[str, Optional[str], Optional[Version]]:
    """Return ``(kind, project_key, version)``; kind is ``wheel``, ``sdist`` or ``other``."""
    try:
        if filename.endswith(".whl"):
            name, version, _, _ = parse_wheel_filename(filename)
            return "wheel", str(name), version
        if filename.endswith((".tar.gz", ".zip")):
            name, version = parse_sdist_filename(filename)
            return "sdist", str(name), version
    except ValueError:  # InvalidWheelFilename, InvalidSdistFilename, InvalidVersion
        pass
    return "other", None, None


def project_from_filename(filename: str) -> Optional[str]:
    """Normalised project name encoded in a wheel or sdist file name, or None."""
    return parse_dist_filename(filename)[1]


def parse_size(text: str) -> int:
    """``"20GB"``, ``"512 MiB"``, ``"1.5g"`` or a plain byte count, as bytes. Units are binary (1 GB = 1024**3)."""
    match = _SIZE_RE.fullmatch(text or "")
    if not match:
        raise ValueError(f"Cannot read {text!r} as a size, try something like 20GB or 512MiB")
    number, prefix, _ = match.groups()
    power = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}[prefix.lower()]
    return int(float(number) * 1024**power)

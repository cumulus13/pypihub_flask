#!/usr/bin/env python3

# File: pypihub/readme.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Safe HTML for a package's long description (Markdown, reStructuredText or plain text)
# License: LGPL-3.0-or-later

"""
Renders README text exactly as pypi.org does, with the same library (``readme_renderer``), which also
sanitises the result: scripts, iframes, event handlers and ``javascript:`` or ``data:`` links are removed.

``readme_renderer`` is optional (``pip install pypihub[readme]``). Without it, or when a README does not
parse, :func:`render` returns None and the page shows the text escaped, never as HTML.
"""

from __future__ import annotations

import logging
import re
from email.message import Message
from typing import Optional

log = logging.getLogger(__name__)

MAX_RENDER_CHARS = 500_000  # larger READMEs are shown as plain text; they would take seconds to render

_IMG = re.compile(r"<img\b")


def available() -> bool:
    try:
        import readme_renderer.markdown  # noqa: F401
        import readme_renderer.rst  # noqa: F401
        import readme_renderer.txt  # noqa: F401
    except ImportError:
        return False
    return True


def _content_type(value: Optional[str]):
    message = Message()
    message["content-type"] = value or "text/x-rst"  # what PyPI assumes when a project declares nothing
    return message.get_content_type(), message.get_param("variant")


def render(raw: Optional[str], content_type: Optional[str]) -> Optional[str]:
    """Sanitised HTML, or None when it cannot be rendered (the caller then shows escaped plain text)."""
    if not raw or not raw.strip() or len(raw) > MAX_RENDER_CHARS:
        return None
    try:
        from readme_renderer import markdown, rst, txt
    except ImportError:
        return None
    kind, variant = _content_type(content_type)
    try:
        if kind == "text/markdown":
            html = markdown.render(raw, variant=(variant or "GFM"))
        elif kind == "text/x-rst":
            html = rst.render(raw)
        else:
            html = txt.render(raw)
        if html is None:  # PyPI falls back to plain text when Markdown or reST does not parse
            html = txt.render(raw)
    except Exception as exc:  # a renderer bug must not break the page
        log.warning("Could not render a description: %s", exc)
        return None
    if not html:
        return None
    # Images load straight from their hosts: do not send our address as referrer, and load them lazily.
    return _IMG.sub('<img loading="lazy" referrerpolicy="no-referrer"', html)

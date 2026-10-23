#!/usr/bin/env python3

# File: pypihub/limits.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Rate limiting (Flask-Limiter) configured from the environment
# License: LGPL-3.0-or-later

"""
Rate limits, per client address.

    PYPIHUB_RATE_LIMIT_DEFAULT         every request, e.g. "600 per minute" (default: no general limit)
    PYPIHUB_RATE_LIMIT_UPLOAD          upload requests (default "60 per minute")
    PYPIHUB_RATE_LIMIT_AUTH_FAILURES   rejected logins and uploads, against password guessing (default "10 per minute")
    PYPIHUB_RATE_LIMIT_ENABLED         set to 0 to switch all limits off

Counters live in Redis when REDIS_URL is set, so every worker and replica shares them. Without Redis
each worker counts separately, which makes the effective limit ``workers * limit``.
Behind a reverse proxy set PYPIHUB_TRUST_PROXY=1, or every client looks like the proxy.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from flask import Flask, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

log = logging.getLogger(__name__)

_OFF = {"", "0", "off", "false", "no", "none"}


def _value(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def enabled() -> bool:
    return _value("PYPIHUB_RATE_LIMIT_ENABLED", "1").lower() not in _OFF


def init_limiter(app: Flask, redis_url: Optional[str] = None) -> Limiter:
    app.config["RATELIMIT_ENABLED"] = enabled()
    default = _value("PYPIHUB_RATE_LIMIT_DEFAULT")
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[default] if default.lower() not in _OFF else [],
        storage_uri=redis_url or "memory://",
        strategy="fixed-window",
        headers_enabled=True,
        swallow_errors=True,  # a Redis outage must not take the site down
        in_memory_fallback_enabled=True,
    )

    @limiter.request_filter
    def _never_limit_health_checks_or_assets() -> bool:
        return request.path == "/healthz" or request.path.startswith("/static/")

    if enabled() and not redis_url:
        log.info("Rate limits are counted per worker process. Set REDIS_URL to share them between workers.")
    return limiter


def upload_limit() -> Optional[str]:
    value = _value("PYPIHUB_RATE_LIMIT_UPLOAD", "60 per minute")
    return None if value.lower() in _OFF else value


def auth_failure_limit() -> Optional[str]:
    value = _value("PYPIHUB_RATE_LIMIT_AUTH_FAILURES", "10 per minute")
    return None if value.lower() in _OFF else value


def apply_limits(limiter: Limiter) -> Callable:
    """Decorator for routes that accept credentials: an overall cap plus a cap on rejected attempts."""

    def decorator(view: Callable) -> Callable:
        if auth_failure_limit():
            view = limiter.limit(auth_failure_limit(), deduct_when=lambda response: response.status_code == 401)(view)
        if upload_limit():
            view = limiter.limit(upload_limit())(view)
        return view

    return decorator

#!/bin/sh
# PyPihub container entrypoint.
#   - checks that the data directories are writable
#   - creates a persistent SECRET_KEY when none is given (sessions then survive restarts and are shared by workers)
#   - creates or updates an administrator when PYPIHUB_ADMIN_USER and a password are given
set -eu

for dir in "$LOCAL_PKG_DIR" "$CACHE_DIR"; do
    mkdir -p "$dir" 2>/dev/null || true
    if [ ! -w "$dir" ]; then
        echo "pypihub: $dir is not writable by uid $(id -u). If /data is a bind mount, run: chown -R $(id -u):$(id -g) <host directory>" >&2
        exit 1
    fi
done

if [ -z "${SECRET_KEY:-}" ]; then
    key_file="${BASE_DIR}/.secret_key"
    if [ ! -s "$key_file" ]; then
        (umask 077 && python -c "import secrets; print(secrets.token_hex(32))" > "$key_file")
    fi
    SECRET_KEY="$(cat "$key_file")"
    export SECRET_KEY
fi

if [ -n "${PYPIHUB_ADMIN_USER:-}" ]; then
    if [ -n "${PYPIHUB_ADMIN_PASSWORD_FILE:-}" ]; then
        PYPIHUB_ADMIN_PASSWORD="$(cat "$PYPIHUB_ADMIN_PASSWORD_FILE")"
    fi
    if [ -z "${PYPIHUB_ADMIN_PASSWORD:-}" ]; then
        echo "pypihub: PYPIHUB_ADMIN_USER is set but neither PYPIHUB_ADMIN_PASSWORD nor PYPIHUB_ADMIN_PASSWORD_FILE is." >&2
        exit 1
    fi
    export PYPIHUB_ADMIN_PASSWORD
    python - <<'PY'
import os
from pypihub.pypihub import create_user_cli

create_user_cli(os.environ["PYPIHUB_ADMIN_USER"], os.environ["PYPIHUB_ADMIN_PASSWORD"])
PY
fi

# Bring the package catalog in line with the files on disk (existing installs, restored backups, manual changes).
# It is incremental and safe to run from several replicas at once.
if [ "${PYPIHUB_REINDEX_ON_START:-1}" != "0" ]; then
    pypihub reindex
fi

exec "$@"

#!/bin/sh
set -eu

# Railway mounts volumes as root. Prepare only the configured SQLite parent
# directory and the temporary compiler sandbox, then drop application privileges.
db_path=${DATABASE_PATH:-/app/codementor.db}
db_dir=$(dirname -- "$db_path")
sandbox_dir=${CODEMENTOR_SANDBOX_DIR:-/tmp/codementor-sandboxes}

case "$db_dir" in
  /|/tmp|/usr|/etc|/var)
    echo "DATABASE_PATH must be inside a writable application or mounted data directory." >&2
    exit 1
    ;;
esac
case "$sandbox_dir" in
  /|/tmp)
    echo "CODEMENTOR_SANDBOX_DIR must be a dedicated subdirectory." >&2
    exit 1
    ;;
esac

mkdir -p "$db_dir" "$sandbox_dir"
chown 10001:10001 "$db_dir" "$sandbox_dir"
if [ -f "$db_path" ]; then
  chown 10001:10001 "$db_path"
fi

exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"

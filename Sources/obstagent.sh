#!/bin/sh

vault_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd -- "$vault_dir" || exit 1

while true; do
    python3 Sources/obstagent.py --vault "$vault_dir" "$@"
    status_code=$?
    [ "$status_code" -eq 75 ] || exit "$status_code"
done

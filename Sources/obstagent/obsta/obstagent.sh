#!/bin/sh
# Re-execute the script from disk after the supervisor has drained all vaults.
module_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
export PYTHONPATH="$module_dir/..${PYTHONPATH:+:$PYTHONPATH}"
export OBSTA_SUPERVISED=1
python_bin=${OBSTA_PYTHON:-python3}
stopping=0
stop() {
    stopping=1
    if [ -n "$supervisor" ]; then
        kill -TERM "$supervisor" 2>/dev/null
    fi
}
trap stop INT TERM
"$python_bin" -P -B -X "pycache_prefix=$module_dir/.obsta-no-cache" -m obsta "$@" &
supervisor=$!
if [ "$stopping" -eq 1 ]; then
    stop
fi
while true; do
    wait "$supervisor"
    status_code=$?
    if ! kill -0 "$supervisor" 2>/dev/null; then
        break
    fi
done
if [ "$stopping" -eq 0 ] && [ "$status_code" -eq 75 ]; then
    exec /bin/sh "$0" "$@"
fi
[ "$stopping" -eq 0 ] || exit 0
exit "$status_code"

#!/usr/bin/env bash
# Run the shroud end-to-end test in headless Chromium.
#   tests/run.sh            # test only
#   tests/run.sh --shots    # also refresh docs/*.png for the README
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
port=7739 cdp=9333
ext_id=$(python3 -c "import hashlib,sys;h=hashlib.sha256(sys.argv[1].encode()).hexdigest()[:32];print(''.join(chr(97+int(c,16)) for c in h))" "$root/veil")
if ss -ltn | grep -q ":$port "; then echo "port $port is busy (stale test server?)" >&2; exit 1; fi
work="$(mktemp -d)"
cleanup() { kill "${srv:-}" "${chr:-}" 2>/dev/null || true; rm -rf "$work"; }
trap cleanup EXIT

"$root/.venv/bin/python" "$root/server.py" --no-radio --port $port >"$work/glass.log" 2>&1 & srv=$!
chromium --headless=new --user-data-dir="$work/profile" --no-first-run --hide-scrollbars \
  --load-extension="$root/veil" --disable-extensions-except="$root/veil" \
  --remote-debugging-port=$cdp about:blank >/dev/null 2>&1 & chr=$!

shots=""
[[ "${1:-}" == "--shots" ]] && shots="$root/docs" && mkdir -p "$shots"
node "$root/tests/shroud.mjs" $cdp "http://127.0.0.1:$port" "$ext_id" $shots

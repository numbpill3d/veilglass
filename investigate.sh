#!/usr/bin/env bash
# Open a throwaway Chromium under the shroud for looking into something the
# glass turned up. Fresh profile every run (no cookies, history or logins
# carried in or out), WebRTC sealed, and the profile is deleted on exit.
#
#   ./investigate.sh                         # direct connection
#   ./investigate.sh --tor                   # through a local Tor (socks5 127.0.0.1:9050)
#   ./investigate.sh --proxy socks5://host:port  https://example.org
#
# Your IP address is still visible unless you use --tor or --proxy.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
proxy=""
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tor) proxy="socks5://127.0.0.1:9050"; shift ;;
    --proxy) proxy="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) args+=("$1"); shift ;;
  esac
done

if [[ "$proxy" == socks5://127.0.0.1:9050 ]] && ! (exec 3<>/dev/tcp/127.0.0.1/9050) 2>/dev/null; then
  echo "no Tor on 127.0.0.1:9050. start it:  sudo systemctl start tor" >&2
  exit 1
fi

profile="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/veilglass-investigate.XXXXXX")"
trap 'rm -rf "$profile"' EXIT

flags=(
  --user-data-dir="$profile"
  --load-extension="$here/veil"
  --disable-extensions-except="$here/veil"
  --no-first-run --no-default-browser-check
  --disable-sync --disable-background-networking
  --force-webrtc-ip-handling-policy=disable_non_proxied_udp
  --webrtc-ip-handling-policy=disable_non_proxied_udp
  --lang=en-US
)
if [[ -n "$proxy" ]]; then
  # resolve hostnames through the proxy too, so DNS doesn't leak around it
  flags+=(--proxy-server="$proxy" --host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1")
fi

chromium "${flags[@]}" "${args[@]:-about:blank}"

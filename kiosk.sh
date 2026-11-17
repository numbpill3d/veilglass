#!/usr/bin/env bash
# Open the glass fullscreen. Alt+F4 to leave.
exec chromium --kiosk --app=http://127.0.0.1:7733 --touch-events=enabled \
  --overscroll-history-navigation=0 --user-data-dir="$HOME/.cache/veilglass-kiosk" "$@"

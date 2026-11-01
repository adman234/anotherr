#!/bin/bash
set -e
mkdir -p /config/models
PUID=${PUID:-99}
PGID=${PGID:-100}
if [ "$PUID" != "0" ]; then
  # only chown what we own; the model cache can be large so skip it once owned
  chown "$PUID:$PGID" /config /config/models 2>/dev/null || true
  find /config -maxdepth 1 -name 'anotherr.db*' -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  if [ "$(stat -c %u /config/models)" = "$PUID" ]; then
    find /config/models ! -user "$PUID" -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python3 -m anotherr
fi
exec python3 -m anotherr

#!/bin/bash
# dashcam_watch.sh - runs forever (as a systemd service). When the dashcam hotspot is up and wlan0 is joined to it,
# runs dashcam_sync.py once, then waits for the hotspot to go away before arming again.
# Needs wlan0 to auto-join the dashcam (saved in wpa_supplicant) - see install notes.
GW=${GW:-192.168.0.1}
DIR=${DIR:-$HOME/dashcam}
SYNC=${SYNC:-$HOME/dashcam_sync.py}
NAS=${NAS:?set NAS=user@host:/path in /etc/default/dashcam-sync}
NASPORT=${NASPORT:-22}
KEY=${KEY:-$HOME/.ssh/dashcam_nas}
CLIP_SECONDS=${CLIP_SECONDS:-60}         # must match the dashcam's loop-recording length setting
SESSIONS=$DIR/hotspot_sessions.log      # one line per hotspot appearance: YYYY-mm-dd HH:MM:SS
mkdir -p "$DIR"; touch "$SESSIONS"

say() { echo "$*"; }                    # journald adds timestamps
up() { ping -c1 -W1 "$GW" >/dev/null 2>&1; }

in_session=0; synced=0; fails=0; last_flush=0
FLUSH_EVERY=${FLUSH_EVERY:-300}          # while the hotspot is away, push stray finished clips to the NAS this often
say "dashcam_watch started; watching $GW"
while true; do
  if up; then
    if [ $in_session = 0 ]; then
      in_session=1; synced=0; fails=0
      prev=$(tail -n1 "$SESSIONS")                  # start of the previous hotspot session = when the car last set off
      date '+%F %T' >> "$SESSIONS"
      say "hotspot UP (previous session started: ${prev:-none})"
    fi
    if [ $synced = 0 ]; then
      args=(--sftp "$NAS" --sftp-port "$NASPORT" --sftp-key "$KEY" --dest "$DIR" --clip-seconds "$CLIP_SECONDS")
      args+=(--sessions "$SESSIONS")
      say "starting sync"
      timeout 3600 python3 "$SYNC" "${args[@]}"
      rc=$?
      say "sync finished rc=$rc"
      if [ $rc = 0 ]; then synced=1; else fails=$((fails+1)); [ $fails -ge 3 ] && synced=1; sleep 10; fi
    fi
  else
    if [ $in_session = 1 ]; then say "hotspot DOWN"; fi
    in_session=0
    now=$(date +%s)
    if [ $((now - last_flush)) -ge $FLUSH_EVERY ]; then
      last_flush=$now
      if find "$DIR" -name '*.MP4' -print -quit 2>/dev/null | grep -q .; then
        timeout 280 python3 "$SYNC" --flush-only --sftp "$NAS" --sftp-port "$NASPORT" --sftp-key "$KEY" --dest "$DIR"
      fi
    fi
  fi
  sleep 5
done

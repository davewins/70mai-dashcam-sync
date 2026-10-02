#!/bin/bash
# dashcam_watch.sh - runs forever (as a systemd service). When the dashcam hotspot is up and wlan0 is joined to it,
# runs dashcam_sync.py once, then waits for the hotspot to go away before arming again.
# Also keeps a dated log file and uploads it (plus a short status.txt) to <NAS base>/_logs/ so you can check on it remotely.
# Needs wlan0 to auto-join the dashcam (saved in wpa_supplicant) - see the README.
GW=${GW:-192.168.0.1}
DIR=${DIR:-$HOME/dashcam}
SYNC=${SYNC:-$HOME/dashcam_sync.py}
NAS=${NAS:?set NAS=user@host:/path in /etc/default/dashcam-sync}
NASPORT=${NASPORT:-22}
KEY=${KEY:-$HOME/.ssh/dashcam_nas}
CLIP_SECONDS=${CLIP_SECONDS:-60}         # must match the dashcam's loop-recording length setting
FLUSH_EVERY=${FLUSH_EVERY:-300}          # while the hotspot is away, push stray finished clips to the NAS this often (seconds)
STATUS_EVERY=${STATUS_EVERY:-3600}       # upload the log and status.txt to the NAS this often when idle (seconds)
SESSIONS=$DIR/hotspot_sessions.log       # one line per hotspot appearance: YYYY-mm-dd HH:MM:SS
LOG=$DIR/dashcam-sync.log                # dated log, uploaded to <NAS base>/_logs/
mkdir -p "$DIR"; touch "$SESSIONS" "$LOG"
export DASHCAM_LOG_DATES=1               # makes dashcam_sync.py put the date on every log line

NASARGS=(--sftp "$NAS" --sftp-port "$NASPORT" --sftp-key "$KEY")

say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }     # also goes to journald
up() { ping -c1 -W1 "$GW" >/dev/null 2>&1; }
trim_log() {                                              # keep the log under about 3 MB
  if [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 3000000 ]; then
    tail -n 20000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}
upload_status() {
  trim_log
  timeout 120 python3 "$SYNC" --upload-status "$LOG" --sessions "$SESSIONS" "${NASARGS[@]}" --dest "$DIR" >/dev/null 2>&1 \
    || say "status upload to the NAS failed (will retry)"
  last_status=$(date +%s)
}

in_session=0; synced=0; fails=0; last_flush=0; last_status=0
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
      say "starting sync"
      timeout 3600 python3 "$SYNC" "${NASARGS[@]}" --dest "$DIR" --clip-seconds "$CLIP_SECONDS" --sessions "$SESSIONS" 2>&1 | tee -a "$LOG"
      rc=${PIPESTATUS[0]}
      say "sync finished rc=$rc"
      if [ $rc = 0 ]; then synced=1; else fails=$((fails+1)); [ $fails -ge 3 ] && synced=1; sleep 10; fi
      upload_status
    fi
  else
    if [ $in_session = 1 ]; then say "hotspot DOWN"; fi
    in_session=0
    now=$(date +%s)
    if [ $((now - last_flush)) -ge $FLUSH_EVERY ]; then
      last_flush=$now
      if find "$DIR" -name '*.MP4' -print -quit 2>/dev/null | grep -q .; then
        timeout 280 python3 "$SYNC" --flush-only "${NASARGS[@]}" --dest "$DIR" 2>&1 | tee -a "$LOG"
      fi
    fi
    if [ $((now - last_status)) -ge $STATUS_EVERY ]; then
      upload_status
    fi
  fi
  sleep 5
done

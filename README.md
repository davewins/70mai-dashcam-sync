# 70mai-dashcam-sync

Automatically copy clips from a **70mai A800SE** dashcam to a NAS, using a Raspberry Pi that sits near where
you park. When the car comes home, the Pi joins the dashcam's WiFi hotspot, downloads the new clips, pushes them to
the NAS over SFTP and deletes its local copy. No phone app, no pairing, no cloud.

It was built and tested with firmware 1.6.97ww and a Raspberry Pi running Debian 13. Nothing here is affiliated
with 70mai, and other models or firmware may behave differently.

```
 car (dashcam hotspot)  --WiFi-->  Raspberry Pi (wlan0)  --ethernet/SFTP-->  NAS (Jellyfin, backups ...)
```

## What it does

- Watches for the dashcam hotspot and runs one sync per appearance (`dashcam_watch.sh`, a systemd service).
- Finds new clips and downloads them with HTTP Range resume, so a dropped connection loses nothing.
- Downloads event/parking clips first, then unfinished downloads, then everything else oldest first (the card
  overwrites its oldest clips first).
- Uses the GPS log to tell clips recorded while driving from clips recorded while the car sat still (for example parked on
  charge with the dashcam awake). Order is: event clips, parking clips, normal clips from driving, then normal clips recorded
  stationary. Normal clips within two clips of an event or parking clip count as driving, since that is the footage around an
  incident. Set `STATIONARY=skip` in `/etc/default/dashcam-sync` to leave stationary normal clips on the card, or
  `STATIONARY=normal` to ignore movement. Event and parking clips are never skipped. A normal clip is only judged once it is
  three minutes old (it is still being recorded before that), and with `skip` it is held back until then.
- Pushes each clip to the NAS in a background thread while the next one downloads, as a `.part` file renamed when
  complete, so media servers never see half a file.
- Works out where it is up to from the NAS itself, so there is nothing to seed and nothing to forget. It can recover
  from a lost state file, a long absence, or clips copied across by hand.
- Pushes any finished clip stranded on the Pi (for example after a NAS outage), even when the hotspot is away.

Clips end up on the NAS as `<base>/YYYY-MM-DD/Normal|Parking|Event/<name>.MP4`.

## How it works (the interesting bit)

The dashcam serves its SD card over plain HTTP on `192.168.0.1:80` (thttpd) **with no authentication**:

```
GET ///mnt/sd/Normal/Front/NO20260930-205828-008704F.MP4        (Range requests work)
```

Directory URLs return 403 and the file list is only available through a signed API that is not reproducible without
the vendor's native library. So the tool finds clips another way.

**Clip names.** `NO|PA|EV` (normal, parking, event) + `YYYYMMDD-HHMMSS` + `-` + a 6-digit counter + `F|R` (front/rear).
The counter increases by exactly 1 for every clip pair, and the rear clip shares the front clip's counter and time.

**The GPS log is an index.** The card holds `GPSData000001.txt` with one line per second, each naming the clip being
recorded, for example `1790845997,A,51.98,-2.14,...,NO20261001-181229-008817F.MP4,0,0,0`. It is served over HTTP, so the
tool reads only the tail with a Range request and gets the exact names of every recent clip.

**Fallbacks.** Counters missing from the log (parking clips, no GPS fix) are found by guessing timestamps with cheap
`HEAD` requests, since only the time part of the name is unknown. If the GPS file is not available at all, it falls
back to that guessing for everything, which needs a `--seed`.

**The NAS is the source of truth.** At the start of each session the tool lists the NAS folders for recent dates, drops
queued clips that are already there, and skips ahead past the newest clip already delivered.

## Limits you should know about

- **WiFi window.** In parking mode (for example with the OBD hardwire kit) the dashcam switches its hotspot off a few
  minutes after the engine stops. The window is roughly 1 to 4 minutes, and the dashcam's WiFi is slow (about 1 to 7 MB/s depending on the
  radio). At HD that is only a handful of one-minute clips per drive, so this will not mirror a long journey on its own.
  The tool takes as much as the window allows, oldest first, and carries on next time. See "Keeping the dashcam awake" below
  for ways to get a longer window.
- **Loop recording.** Unlocked clips are overwritten when the card fills. A minute of HD front plus rear is about 170 MB, so a
  120 GB card holds roughly 12 hours of continuous recording, and a dashcam that is awake but parked still records at that rate.
  Clips the Pi never got are only recoverable from the card. `import_card.py` copies a card in a reader at disk speed.
- **A clip that will not download** (a trickle of data, or a stall) is abandoned after 20 seconds below about 100 KB/s, sent to the
  back of the queue, and tried once per session. After 3 failed sessions it is given up on and listed in `status.txt`; copy it from
  the card by hand (`import_card.py`) and the next sync notices it is on the NAS. A dead hotspot never counts against a clip.
- WiFi range matters a lot, see "Getting a fast link" below.
- Clips are recorded in the dashcam's local time and the tool assumes the Pi is in the same time zone.

## Keeping the dashcam awake

The hotspot only exists while the dashcam is awake and not in parking mode. Two things keep it up for hours:

- **Charging a plug-in hybrid or EV.** The car is awake while it charges, so the dashcam stays in normal mode and the hotspot
  can stay up for hours. That is the best time to sync, but it also records about 10 GB an hour of the stationary garage, which
  is why `STATIONARY=skip` is worth having.
- **A USB battery pack** on the dashcam, with the car off. In the dashcam's settings, "auto off when parked" can be set to
  "never" for this. A 3 to 5 W dashcam runs for several hours on a typical power bank. Use the wired output, not a wireless
  charging pad, and remember that the card is being overwritten at 10 GB an hour the whole time, so use it to catch up and then
  put the dashcam back on car power. Setting "never" may also stop parking mode (and so Parking/Event clips): test it first.

## Getting a fast link

The dashcam's hotspot is 2.4 GHz only, so range and position matter far more than the radio's headline speed. Measured on one Pi:
about 14 KB/s with the Pi behind a metal garage door, 0.4 to 1.7 MB/s a metre or two away from the metal, and 2 to 2.6 MB/s with an
external USB adapter and a clear line of sight (the dashcam itself managed 6.9 MB/s to a laptop held next to it).

- Put the Pi, or just its aerial, where it can see the car without metal in between. Measure rather than guess:
  `iw dev wlan0 link` (look at `signal` and `rx bitrate`; better than -65 dBm is good, -78 dBm is slow) and
  `curl -m 15 -o /dev/null -w "%{speed_download} bytes/s\n" -r 0-5000000 "http://192.168.0.1///mnt/sd/GPSData000001.txt"`
  (stop the service first so the two do not compete).
- The dashcam picks its own channel and has been seen on both 2 and 11. A strong neighbour on the same channel slows it.
- A USB adapter with an external aerial helps if the Pi's own radio is the weak point. Choose one with a driver already in the
  Linux kernel (MediaTek MT7612U or MT7610U, Ralink RT5372, Atheros AR9271) and avoid Realtek and AIC8800 dongles that need a driver
  compiled on the Pi: it is rebuilt on every kernel update and breaks silently. A short USB extension lets you place the aerial.
- With a second adapter, join the dashcam on the new interface only, and set `WIFI_IF` (for example `wlan1`) in
  `/etc/default/dashcam-sync` so power saving is switched off on the right one. The watcher finds the dashcam by pinging it, so it
  works on any interface.

## Checking on it remotely

The Pi keeps a dated log (`~/dashcam/dashcam-sync.log`, trimmed to about 3 MB) and, after every sync and once an hour, uploads
two files to the NAS next to your clips:

```
<NAS base>/_logs/status.txt          a short summary: newest clip, queue length, clips delivered in 24 h / 7 days,
                                     failures, hotspot sessions and how long each stayed alive, and WARNING lines
<NAS base>/_logs/dashcam-sync.log    the recent log
```

The log also records how long the dashcam's hotspot stayed up, for example
`hotspot DOWN after 3m41s (up 18:34:21, last seen 18:38:02)`. A background ping once a second times it, so the figure is accurate
even though the main loop is busy syncing. Durations are kept in `~/dashcam/hotspot_durations.log` and summarised in `status.txt`.

Open them from wherever you already reach the NAS (a file browser, VPN, SFTP, a phone). `status.txt` warns when the hotspot has
appeared in the last 24 hours but nothing was delivered, or when more than 40 clips are queued (the card only holds about
12 hours of recording, so it is time to pull it). If `status.txt` stops updating, the Pi, its network or the NAS is down. Tune with `STATUS_EVERY`
(seconds, default 3600).

## Install on the Pi

Requirements: Python 3.8+ (standard library only), OpenSSH `sftp`, `iw`, and WiFi (`wlan0` by default, see `WIFI_IF`) that
auto-joins the dashcam network. Debian with `wpa_supplicant` and `dhcpcd` is what it was tested on.

1. Join the dashcam's hotspot automatically, but keep ethernet as the default route. For `dhcpcd`, add `nogateway`
   for `wlan0` in `/etc/dhcpcd.conf`. Add the dashcam's SSID and password to `/etc/wpa_supplicant/wpa_supplicant.conf`.
2. Create an SSH key for the NAS and authorise it for the account that will receive the clips:
   `ssh-keygen -t ed25519 -f ~/.ssh/dashcam_nas -N ""`.
3. Copy the files and put the settings in `/etc/default/dashcam-sync`:

   ```bash
   cp dashcam_sync.py dashcam_watch.sh ~/ && chmod +x ~/dashcam_watch.sh
   sudo cp dashcam-sync.service /etc/systemd/system/
   sudo tee /etc/default/dashcam-sync <<'CFG'
   NAS=user@nas.local:/path/to/Dashcam
   NASPORT=22
   KEY=/home/pi/.ssh/dashcam_nas
   CLIP_SECONDS=60
   CFG
   ```

   `CLIP_SECONDS` must match the dashcam's loop-recording length. `GW` (default `192.168.0.1`), `DIR`
   (default `~/dashcam`, the staging folder, state and log), `SYNC` (path to `dashcam_sync.py`, default `~/dashcam_sync.py`),
   `FLUSH_EVERY`, `STATUS_EVERY`, `STATIONARY` and `WIFI_IF` (default `wlan0`) can also be set there.
4. Start it and watch it work:

   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now dashcam-sync
   journalctl -u dashcam-sync -f
   ```

The first time the hotspot appears, the tool looks at the card's GPS log and the NAS and starts after the newest clip
already delivered. For a brand new NAS folder it starts from the oldest clip still in the log. If the GPS log is not
available, run `python3 dashcam_sync.py --seed NO20260930-205828-008704F.MP4 ...` once with the name of your newest clip.

## Updating

If you cloned this repository on the Pi, update with `git pull`, copy `dashcam_sync.py` and `dashcam_watch.sh` to your home
folder (or point `SYNC` at the clone), and run `sudo systemctl restart dashcam-sync`. The state lives in `~/dashcam`, so
nothing is lost.

## Command line

```
dashcam_sync.py [--gw IP] [--dest DIR] [--state FILE]
                [--sftp user@host:/base --sftp-port N --sftp-key FILE]
                [--seed CLIPNAME] [--types EV,PA,NO] [--clip-seconds 60]
                [--stationary last|skip|normal] [--moving-metres 40] [--no-gps] [--sessions FILE] [--dry-run] [--flush-only]
                [--upload-status LOGFILE]
```

- `--dry-run` finds and lists new clips but downloads nothing.
- `--flush-only` pushes finished clips left in `--dest` to the NAS and does not touch the dashcam.
- `--upload-status LOGFILE` writes `status.txt` and uploads it, with the log, to `<NAS base>/_logs/`, then exits.
- Without `--sftp`, clips simply stay in `--dest`.
- `--sessions` is a file of times the hotspot appeared (the watcher writes one). It is only used by the guessing
  fallback, to know when the dashcam was on.

`import_card.py CARD DEST [--min-counter N]` copies clips from the SD card in a reader into the same
`YYYY-MM-DD/Type/` layout, skipping files already there with the same size.

## Tests

`tests/fake.py` is a tiny fake dashcam (including a GPS log with `--gps`) and `tests/fakesftp.py` is a fake `sftp`:

```bash
python3 tests/fake.py --gps &                       # serves on 127.0.0.1:8099
export SFTP_BIN="python3 $PWD/tests/fakesftp.py"
mkdir nas
python3 dashcam_sync.py --gw 127.0.0.1 --port 8099 --dest /tmp/pi --sftp u@h:/base --seed <clip name printed by fake.py>
```

## Licence

MIT. Use at your own risk, and only on a dashcam that is yours.

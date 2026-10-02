#!/usr/bin/env python3
"""dashcam_sync.py - copy new 70mai A800SE clips to a folder, no pairing/signing needed.

The dashcam serves video files over plain HTTP without authentication, but hides the file list behind a signed
API. Clip names are NO|PA|EV + YYYYMMDD-HHMMSS + -<counter> + F|R, and the counter goes up by exactly 1 for every
new clip (all types share it). So we keep the last counter we saw and look for counter+1 by trying likely
timestamps with cheap HEAD requests.

First run:   ./dashcam_sync.py --seed NO20260930-205828-008704F.MP4     (newest clip you already have)
Afterwards:  ./dashcam_sync.py          (state is kept in STATE file)
Options:     --dry-run  find clips but download nothing      --dest DIR     --gw 192.168.0.1
"""
import argparse, datetime as dt, http.client, json, os, re, shlex, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

FOLDER = {"NO": "Normal", "PA": "Parking", "EV": "Event"}
PRIORITY = {"EV": 0, "PA": 1, "NO": 2}          # download order
NAME_RE = re.compile(r"^(NO|PA|EV)(\d{8}-\d{6})-(\d{6})[FR]\.MP4$")
TFMT = "%Y%m%d-%H%M%S"

def log(*a):
    fmt = "%Y-%m-%d %H:%M:%S" if os.environ.get("DASHCAM_LOG_DATES") else "%H:%M:%S"   # the watcher's log file wants dates
    print(time.strftime(fmt), *a, flush=True)

class Cam:
    def __init__(self, gw, port):
        self.gw, self.port = gw, port
    def req(self, method, path, headers=None, timeout=6):
        c = http.client.HTTPConnection(self.gw, self.port, timeout=timeout)
        try:
            c.request(method, path, headers=headers or {})
            r = c.getresponse()
            return c, r
        except Exception:
            c.close()
            raise
    def head(self, path):
        for attempt in (1, 2):
            try:
                c, r = self.req("HEAD", path)
                try:
                    if r.status in (200, 206):
                        return int(r.getheader("Content-Length", "-1"))
                    return None
                finally:
                    c.close()
            except (OSError, http.client.HTTPException):
                if attempt == 2:
                    raise

def path_for(prefix, t, counter, cam):          # cam = 'F' or 'R'
    name = f"{prefix}{t:{TFMT}}-{counter:06d}{cam}.MP4"
    return f"///mnt/sd/{FOLDER[prefix]}/{'Front' if cam == 'F' else 'Rear'}/{name}", name

def find_next(cam, counter, prev_t, now, since, pool, anchors=(), max_search=0, full=False, clip_s=60):
    """Look for the clip with this counter. Returns (prefix, time, size) or None."""
    def probe(c):
        prefix, t = c
        p, _ = path_for(prefix, t, counter, "F")
        size = cam.head(p)
        return (prefix, t, size) if size is not None else None
    def run(cands):
        for i in range(0, len(cands), 32):
            for r in pool.map(probe, cands[i:i + 32]):
                if r:
                    return r
        return None
    sec = lambda base, a, b: [base + dt.timedelta(seconds=s) for s in range(a, b)]
    # stage 1: the usual clip length after the previous clip start (Normal)
    r = run([("NO", t) for t in sec(prev_t, clip_s - 5, clip_s + 6)])
    if r: return r
    # stage 1b: a gap (dashcam was off). The dashcam boots when the car is started and its hotspot appears about
    # 25 s later (measured), so a recording chain starts shortly before one of the hotspot sessions logged since the
    # last clip. Sessions at arrival (car approaching home) are no use, but they cost only a few seconds each; try the
    # earliest first, because the first one after the last clip is normally the departure.
    tried = 0
    for a in anchors:
        if a <= prev_t + dt.timedelta(seconds=30) or tried >= 12:
            continue
        tried += 1
        r = run([("NO", t) for t in sec(a, -150, 31)])
        if r: return r
    if not full:
        return None
    # stage 2 (slow): any type, up to 7.5 min after the previous clip start (parking/event clips, restarts)
    r = run([(p, t) for t in sec(prev_t, 1, max(630, clip_s + 570)) for p in (("NO", "PA", "EV") if t < prev_t + dt.timedelta(seconds=max(630, clip_s + 570)) else ("PA", "EV"))])
    if r: return r
    # stage 3 (optional, --max-search): blind search for a Normal clip back from now
    lo = max(prev_t + dt.timedelta(seconds=max(630, clip_s + 570)), since, now - dt.timedelta(seconds=max_search))
    n = int((now - lo).total_seconds())
    if max_search and n > 0:
        r = run([("NO", t) for t in sec(lo, 0, n + 1)])
        if r: return r
    return None

GPS_PATH = "///mnt/sd/GPSData000001.txt"
GPS_NAME = re.compile(r"\b(NO|PA|EV)(\d{8}-\d{6})-(\d{6})F\.MP4")

def gps_index(cam, last_counter, max_bytes=16 << 20, older_than=None):
    """The dashcam logs one GPS line per second, each naming the clip being recorded. Read the tail of that file and
    return {counter: (prefix, time)} for clips newer than last_counter. Returns None if the file is not available."""
    size = cam.head(GPS_PATH)
    if not size or size < 0:
        return None
    chunk = 256 << 10
    while True:
        start = max(0, size - chunk)
        c, r = cam.req("GET", GPS_PATH, {"Range": f"bytes={start}-{size - 1}"}, timeout=20)
        try:
            data = r.read().decode("latin-1")
            ok = r.status in (200, 206)
        finally:
            c.close()
        if not ok:
            return None
        found = {}
        for m in GPS_NAME.finditer(data):
            found[int(m.group(3))] = (m.group(1), dt.datetime.strptime(m.group(2), TFMT))
        if older_than is not None:
            done = bool(found) and min(v[1] for v in found.values()) <= older_than
        else:
            done = bool(found) and min(found) <= last_counter
        if start == 0 or done or chunk >= max_bytes:
            break
        chunk *= 4
    return {k: v for k, v in found.items() if k > last_counter}

def download(cam, path, dest, size):
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        return
    if have > size:
        os.remove(part); have = 0
    if have < size:
        c, r = cam.req("GET", path, {"Range": f"bytes={have}-"}, timeout=20)
        try:
            if r.status not in (200, 206):
                raise OSError(f"HTTP {r.status}")
            if r.status == 200 and have:          # server ignored the Range header
                have = 0
            with open(part, "ab" if have else "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b: break
                    f.write(b)
        finally:
            c.close()
    if os.path.getsize(part) != size:
        raise OSError(f"incomplete: {os.path.getsize(part)} of {size} bytes")
    os.replace(part, dest)


SFTP_EXTRA = []

def sftp_push(target, local, remote_rel):
    """target 'user@host:/base/dir'. Uploads to a .part name and renames, so Jellyfin never sees half a file."""
    host, base = target.split(":", 1)
    remote = base.rstrip("/") + "/" + remote_rel
    cmds, parts = [], remote.split("/")
    for i in range(2 if remote.startswith("/") else 1, len(parts)):   # make every missing folder (errors ignored by '-')
        cmds.append("-mkdir " + shlex.quote("/".join(parts[:i])) if parts[:i] and "/".join(parts[:i]) else "")
    cmds += [f"put {shlex.quote(local)} {shlex.quote(remote + '.part')}",
             f"rename {shlex.quote(remote + '.part')} {shlex.quote(remote)}"]
    sftp = shlex.split(os.environ.get("SFTP_BIN", "sftp -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"))
    sftp += SFTP_EXTRA
    r = subprocess.run(sftp + ["-b", "-", host], input="\n".join(c for c in cmds if c) + "\n",
                       text=True, capture_output=True)
    if r.returncode != 0:
        raise OSError("sftp failed: " + (r.stderr or r.stdout).strip()[-300:])

def upload_status(target, dest, logfile, sessions):
    """Write a short human-readable status.txt and a copy of the recent log to <NAS base>/_logs/ so the Pi can be checked
    remotely. The log lines start with 'YYYY-MM-DD HH:MM:SS' (the watcher sets DASHCAM_LOG_DATES)."""
    now = dt.datetime.now()
    lines = []
    if os.path.exists(logfile):
        with open(logfile, errors="replace") as f:
            lines = f.read().splitlines()[-5000:]
    def stamp(l):
        try: return dt.datetime.strptime(l[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError: return None
    def count(word, hours):
        n = 0
        for l in lines:
            t = stamp(l)
            if t and (now - t).total_seconds() < hours * 3600 and re.search(r"\b" + word + r"\b", l[20:]):
                n += 1
        return n
    sp = os.path.join(dest, "state.json")
    st = json.load(open(sp)) if os.path.exists(sp) else {}
    pend = st.get("pending", [])
    stray = sum(1 for root, _, fs in os.walk(dest) for f in fs if NAME_RE.match(f))
    sess = []
    if sessions and os.path.exists(sessions):
        for l in open(sessions):
            try: sess.append(dt.datetime.strptime(l.strip(), "%Y-%m-%d %H:%M:%S"))
            except ValueError: pass
    s24 = [t for t in sess if (now - t).total_seconds() < 86400]
    out = [f"Dashcam sync status at {now:%Y-%m-%d %H:%M:%S}", ""]
    out.append(f"Newest clip seen: #{st.get('counter')} at {st.get('time')}")
    out.append("Waiting in the queue: " + (f"{len(pend)} clips (#{min(p['counter'] for p in pend)}..#{max(p['counter'] for p in pend)})" if pend else "none"))
    out.append(f"Finished clips stranded on the Pi (not yet on the NAS): {stray}")
    pushed24 = count("pushed", 24) + count("flushed", 24)
    out.append(f"Clips delivered to the NAS: last 24 h {pushed24}, last 7 days {count('pushed', 168) + count('flushed', 168)}")
    out.append(f"Failures: last 24 h {count('FAILED', 24)}")
    out.append(f"Hotspot sessions: last 24 h {len(s24)}; latest {sess[-1] if sess else 'never'}")
    durs = []
    dp = os.path.join(dest, "hotspot_durations.log")
    if os.path.exists(dp):
        for l in open(dp):
            try: durs.append((l[:19], int(l.split()[-1])))
            except ValueError: pass
    if durs:
        fm = lambda n: f"{n // 60}m{n % 60:02d}s"
        out.append("Hotspot alive time, last 5 sessions: " + ", ".join(f"{fm(n)} ({d[11:16]})" for d, n in durs[-5:]))
        recent = [n for _, n in durs[-20:]]
        out.append(f"Hotspot alive time, average of the last {len(recent)}: {fm(sum(recent) // len(recent))}, longest {fm(max(recent))}")
    if s24 and not pushed24:
        out.append("")
        out.append(f"WARNING: the hotspot appeared {len(s24)} times in the last 24 h but nothing was delivered.")
    if len(pend) > 40:
        out.append("")
        out.append(f"WARNING: {len(pend)} clips are queued. The card keeps about a day of footage, so pull the SD card soon.")
    out += ["", "Last 15 log lines:"] + lines[-15:]
    tmp = os.path.join(dest, ".status.tmp")
    tmplog = os.path.join(dest, ".log.tmp")
    open(tmp, "w").write("\n".join(out) + "\n")
    open(tmplog, "w").write("\n".join(lines) + "\n")
    try:
        sftp_push(target, tmp, "_logs/status.txt")
        sftp_push(target, tmplog, "_logs/dashcam-sync.log")
    finally:
        for p in (tmp, tmplog):
            if os.path.exists(p): os.remove(p)

def flush_local(target, dest):
    """Push every finished clip sitting under dest (YYYY-MM-DD/Type/NAME.MP4) to the NAS and delete the local copy.
    Needs no dashcam connection. Returns (pushed, failed)."""
    pushed = failed = 0
    for root, _, files in sorted(os.walk(dest)):
        for f in sorted(files):
            if not NAME_RE.match(f):
                continue                                  # .part files, state.json, logs ...
            local = os.path.join(root, f)
            rel = os.path.relpath(local, dest)
            if len(rel.split(os.sep)) != 3:
                continue
            try:
                sftp_push(target, local, rel.replace(os.sep, "/"))
                os.remove(local)
                pushed += 1
                log(f"flushed {rel} to the NAS")
            except OSError as e:
                failed += 1
                log(f"FAILED to flush {rel}: {e}")
                break                                     # the NAS is probably unreachable: stop, try again later
        if failed:
            break
    return pushed, failed

def list_existing(target, dest, dates):
    """Names of clips already delivered: on the NAS (sftp) or, without --sftp, under dest. Returns {counter: time-string}."""
    names = []
    if target:
        host, base = target.split(":", 1)
        cmds = [f"-ls -1 {shlex.quote(base.rstrip('/') + '/' + d + '/' + f)}" for d in sorted(dates) for f in FOLDER.values()]
        sftp = shlex.split(os.environ.get("SFTP_BIN", "sftp -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new")) + SFTP_EXTRA
        r = subprocess.run(sftp + ["-b", "-", host], input="\n".join(cmds) + "\n", text=True, capture_output=True)
        if r.returncode != 0 and not r.stdout.strip():
            raise OSError("sftp listing failed: " + r.stderr.strip()[-300:])
        names = [ln.strip().split("/")[-1] for ln in r.stdout.splitlines()]
    else:
        for root, _, files in os.walk(dest):
            names += files
    out = {}
    for n in names:
        m = NAME_RE.match(n)
        if m: out[int(m.group(3))] = m.group(2)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw", default=os.environ.get("GW", "192.168.0.1"))
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--dest", default=os.path.expanduser("~/dashcam"))
    ap.add_argument("--state", default=None)
    ap.add_argument("--seed", help="name of a clip you already have (newest one); sets where discovery starts")
    ap.add_argument("--since", help="earliest time the dashcam could have been on, 'YYYY-mm-dd HH:MM:SS' (default: 3 h ago)")
    ap.add_argument("--anchor", help="a time the hotspot appeared, 'YYYY-mm-dd HH:MM:SS' (can also use --sessions)")
    ap.add_argument("--sessions", help="file with one hotspot-appeared time per line, 'YYYY-mm-dd HH:MM:SS' (written by dashcam_watch.sh)")
    ap.add_argument("--max-search", type=int, default=0, help="if >0: also search blindly this many seconds back from now for a Normal clip")
    ap.add_argument("--clip-seconds", type=int, default=60, help="the dashcam's loop-recording length (its 'splittime' setting), default 60")
    ap.add_argument("--types", default="EV,PA,NO", help="clip types to download, comma list of EV,PA,NO")
    ap.add_argument("--sftp", help="push each finished clip to user@host:/remote/base/dir then delete the local copy")
    ap.add_argument("--sftp-port", type=int, default=22)
    ap.add_argument("--sftp-key", help="private key file for the SFTP login")
    ap.add_argument("--flush-only", action="store_true", help="just push finished clips left in --dest to the NAS; do not touch the dashcam")
    ap.add_argument("--upload-status", metavar="LOGFILE", help="upload status.txt and the recent log to <NAS base>/_logs/ and exit")
    ap.add_argument("--no-gps", action="store_true", help="do not use the dashcam's GPS log as a clip index")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-clips", type=int, default=500)
    a = ap.parse_args()
    os.makedirs(a.dest, exist_ok=True)
    SFTP_EXTRA[:] = ["-P", str(a.sftp_port)] + (["-i", os.path.expanduser(a.sftp_key)] if a.sftp_key else [])
    if a.upload_status:
        if not a.sftp: sys.exit("--upload-status needs --sftp")
        try:
            upload_status(a.sftp, a.dest, a.upload_status, a.sessions)
        except OSError as e:
            sys.exit(f"status upload failed: {e}")
        sys.exit(0)
    if a.flush_only or (a.sftp and not a.dry_run):
        if not a.sftp:
            sys.exit("--flush-only needs --sftp")
        pushed, failed = flush_local(a.sftp, a.dest)          # anything already on the Pi but not on the NAS goes first
        if a.flush_only:
            if pushed or failed: log(f"flush: {pushed} pushed, {failed} failed")
            sys.exit(1 if failed else 0)
    sp = a.state or os.path.join(a.dest, "state.json")
    st = json.load(open(sp)) if os.path.exists(sp) else {"counter": None, "time": None, "pending": []}
    def save():
        json.dump(st, open(sp + ".tmp", "w"), indent=1); os.replace(sp + ".tmp", sp)
    if a.seed:
        m = NAME_RE.match(a.seed)
        if not m: sys.exit("bad --seed name")
        st.update(counter=int(m.group(3)), time=m.group(2)); save()
    now = dt.datetime.now()
    since = dt.datetime.strptime(a.since, "%Y-%m-%d %H:%M:%S") if a.since else now - dt.timedelta(hours=3)
    anchors = []
    if a.anchor:
        anchors.append(dt.datetime.strptime(a.anchor, "%Y-%m-%d %H:%M:%S"))
    if a.sessions and os.path.exists(a.sessions):
        for line in open(a.sessions):
            try: anchors.append(dt.datetime.strptime(line.strip(), "%Y-%m-%d %H:%M:%S"))
            except ValueError: pass
    anchors.sort()
    merged = []                                   # hotspot flapping gives several lines within a minute or two: keep the first
    for t in anchors:
        if not merged or (t - merged[-1]).total_seconds() > 120:
            merged.append(t)
    anchors = merged
    cam = Cam(a.gw, a.port)
    try:
        cam.head("///mnt/sd/")                                   # reachability check
    except (OSError, http.client.HTTPException) as e:
        sys.exit(f"dashcam not reachable: {e}")
    if st["counter"] is None or a.sftp:
        # The NAS is the source of truth. With no saved position (first run / state lost) start after the newest clip
        # already delivered; with one, still skip ahead if the NAS is further on (e.g. clips copied by hand).
        fresh = st["counter"] is None
        g = None
        if not a.no_gps:
            g = gps_index(cam, -1 if fresh else st["counter"],
                          older_than=dt.datetime.now() - dt.timedelta(hours=30) if fresh else None)
        if fresh and not g:
            sys.exit("No state yet and the dashcam's GPS log is not available: run once with --seed <newest clip you already have>")
        if g or (st["pending"] and a.sftp):
            try:
                dates = {v[1].strftime("%Y-%m-%d") for v in (g or {}).values()}
                dates |= {dt.datetime.strptime(p["time"], TFMT).strftime("%Y-%m-%d") for p in st["pending"]}
                have = list_existing(a.sftp, a.dest, dates)
            except OSError as e:
                if fresh: sys.exit(str(e))
                log(f"could not list the NAS ({e}); carrying on with the saved position")
                have = {}
            newest = max(have) if have else None
            if not fresh and a.sftp and st["pending"]:        # drop queued clips that are already on the NAS
                keep = [p for p in st["pending"] if p["counter"] not in have]
                if len(keep) != len(st["pending"]):
                    log(f"{len(st['pending']) - len(keep)} queued clips are already on the NAS; dropping them")
                    st["pending"] = keep; save()
            if g and newest is not None and newest > max(g):
                log(f"warning: the NAS has #{newest} but the card's newest is #{max(g)} (card replaced/reset?); ignoring the NAS")
                newest = None
            if not g:
                pass
            elif fresh:
                if newest is None or newest < min(g) - 1:
                    if newest is not None:
                        log(f"warning: newest delivered clip is #{newest} but the card log only reaches back to #{min(g)}; clips in between are lost")
                    newest = min(g) - 1
                tstr = have.get(newest) or (g[newest][1].strftime(TFMT) if newest in g else min(v[1] for v in g.values()).strftime(TFMT))
                st.update(counter=newest, time=tstr); save()
                log(f"no saved position: starting after #{newest}")
            elif newest is not None and newest > st["counter"]:
                log(f"the NAS already has up to #{newest}; skipping ahead from #{st['counter']}")
                st.update(counter=newest, time=have[newest])
                st["pending"] = [p for p in st["pending"] if p["counter"] > newest]
                save()
    want = a.types.split(",")
    state = {"counter": st["counter"], "prev_t": dt.datetime.strptime(st["time"], TFMT)}
    log(f"last known clip: #{state['counter']} at {state['prev_t']}")
    t0 = time.time()

    def add(prefix, t, size):
        state["counter"] += 1; state["prev_t"] = t
        rear = cam.head(path_for(prefix, t, state["counter"], "R")[0])
        st["pending"].append({"prefix": prefix, "time": t.strftime(TFMT), "counter": state["counter"],
                              "F": size, "R": rear})
        st["counter"], st["time"] = state["counter"], t.strftime(TFMT)
        save()
        log(f"found #{state['counter']} {FOLDER[prefix]} {t} front={size/1e6:.1f}MB rear={'-' if rear is None else f'{rear/1e6:.1f}MB'}")

    gps = {}
    if not a.no_gps:
        try:
            gps = gps_index(cam, state["counter"]) or {}
            log("GPS log not available" if not gps else f"GPS log index: {len(gps)} newer clips (#{min(gps)}..#{max(gps)})")
        except (OSError, http.client.HTTPException) as e:
            log(f"GPS log not usable ({e}); falling back to guessing")

    def discover(pool, full):
        n = 0
        while n < a.max_clips:
            nxt = state["counter"] + 1
            if gps:
                later = [k for k in gps if k >= nxt]
                if later:
                    k = nxt if nxt in gps else None
                    if k is None:                          # counter missing from the GPS log (no fix?): try guessing it, else skip it
                        r = find_next(cam, nxt, state["prev_t"], now, since, pool, anchors, 0, True, a.clip_seconds)
                        if r:
                            add(*r); n += 1; continue
                        k = min(later)
                        log(f"clip(s) #{nxt}..#{k-1} not in GPS log and not found; skipping")
                        state["counter"] = k - 1
                    prefix, t = gps[k]
                    size = cam.head(path_for(prefix, t, k, "F")[0])
                    if size is None:
                        log(f"#{k} is in the GPS log but not on the card (overwritten?); skipping")
                        state["counter"] = k; state["prev_t"] = t; st["counter"], st["time"] = k, t.strftime(TFMT); save()
                        continue
                    add(prefix, t, size); n += 1; continue
            r = find_next(cam, state["counter"] + 1, state["prev_t"], now, since, pool, anchors, a.max_search, full, a.clip_seconds)
            if not r:
                break
            prefix, t, size = r
            state["counter"] += 1; state["prev_t"] = t; n += 1
            rear = cam.head(path_for(prefix, t, state["counter"], "R")[0])
            st["pending"].append({"prefix": prefix, "time": t.strftime(TFMT), "counter": state["counter"],
                                  "F": size, "R": rear})
            st["counter"], st["time"] = state["counter"], t.strftime(TFMT)
            save()
            log(f"found #{state['counter']} {FOLDER[prefix]} {t} front={size/1e6:.1f}MB rear={'-' if rear is None else f'{rear/1e6:.1f}MB'}")
        return n

    def deliver():
        """Download (and push) everything pending. Returns False if something failed.
        Pushes to the NAS run in a background thread so the dashcam's WiFi is never idle waiting for the NAS."""
        newest = max((p["counter"] for p in st["pending"]), default=None)
        def has_part(p):                      # a half-downloaded clip resumes first so it actually finishes
            t = dt.datetime.strptime(p["time"], TFMT)
            return os.path.exists(os.path.join(a.dest, t.strftime("%Y-%m-%d"), FOLDER[p["prefix"]],
                                               path_for(p["prefix"], t, p["counter"], "F")[1] + ".part"))
        # Locked event/parking clips first, then unfinished downloads, then oldest first: the card overwrites its
        # oldest clips first when it fills, so those are the ones at risk.
        todo = sorted((p for p in st["pending"] if p["prefix"] in want),
                      key=lambda p: (PRIORITY[p["prefix"]], not has_part(p), p["counter"]))
        log(f"{len(st['pending'])} clips pending, {len(todo)} of wanted types")
        if a.dry_run:
            return True
        done_files, inflight, pusher, ok = {}, [], (ThreadPoolExecutor(1) if a.sftp else None), True

        def check(p):                         # remove a clip from the queue once every file of it is delivered
            need = {c for c in ("F", "R") if p[c]}
            if need <= done_files.get(p["counter"], set()) and p in st["pending"]:
                st["pending"].remove(p); save()
        def push_and_delete(local, rel):
            sftp_push(a.sftp, local, rel)
            os.remove(local)
        def reap(block):
            nonlocal ok
            for item in list(inflight):
                p, cam_id, name, fut = item
                if block or fut.done():
                    inflight.remove(item)
                    try:
                        fut.result()
                        log(f"pushed {name}")
                        done_files.setdefault(p["counter"], set()).add(cam_id); check(p)
                    except (OSError, http.client.HTTPException) as e:
                        log(f"FAILED to push {name}: {e} - will retry next time")
                        ok = False
        def run():
            for p in todo:
                t = dt.datetime.strptime(p["time"], TFMT)
                if p["counter"] == newest and (now - t).total_seconds() < 180:
                    log(f"skip #{p['counter']} (probably still recording)"); continue
                for cam_id in ("F", "R"):
                    if not p[cam_id]: continue
                    path, name = path_for(p["prefix"], t, p["counter"], cam_id)
                    d = os.path.join(a.dest, t.strftime("%Y-%m-%d"), FOLDER[p["prefix"]])
                    os.makedirs(d, exist_ok=True)
                    local = os.path.join(d, name)
                    try:
                        s0 = time.time()
                        size = cam.head(path)                   # size when found may be stale (clip was still recording)
                        if size is None:
                            log(f"{name} is gone from the card (overwritten?); skipping it")
                            p[cam_id] = None
                            continue
                        p[cam_id] = size
                        have0 = os.path.getsize(local + ".part") if os.path.exists(local + ".part") else 0
                        download(cam, path, local, size)
                        log(f"got {name} {size/1e6:.1f}MB at {(size-have0)/1e6/max(time.time()-s0,.01):.1f}MB/s")
                    except (OSError, http.client.HTTPException) as e:
                        log(f"FAILED {name}: {e} - will resume next time")
                        return False
                    if pusher:
                        while inflight and ok:                  # at most one push queued behind the one running
                            reap(True)
                        if not ok:
                            return False
                        inflight.append((p, cam_id, name, pusher.submit(push_and_delete, local, f"{t:%Y-%m-%d}/{FOLDER[p['prefix']]}/{name}")))
                        reap(False)
                    else:
                        done_files.setdefault(p["counter"], set()).add(cam_id)
                check(p)
                if not ok:
                    return False
            return ok
        try:
            r = run()
        finally:
            if pusher:
                reap(True)                                       # let the last upload finish
                pusher.shutdown()
        return r and ok

    try:
      with ThreadPoolExecutor(8) as pool:
        while True:
              n = discover(pool, full=False)            # cheap: next clip is ~60 s after the last one
              if not deliver():
                  break
              if n:
                  continue
              log("quick search found nothing new; doing the slow search (parking/event clips)")
              if not discover(pool, full=True):
                  log(f"caught up ({time.time() - t0:.0f}s)")
                  break
    except (OSError, http.client.HTTPException) as e:
        log(f"dashcam went away: {e}")
        sys.exit(1)
    log("done")

if __name__ == "__main__":
    main()

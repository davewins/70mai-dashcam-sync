#!/usr/bin/env python3
"""import_card.py - copy clips from the dashcam's SD card (in a card reader) into the same folder layout the WiFi tools use.

    python3 import_card.py /Volumes/<CARD NAME> ~/DashcamBackup
    python3 import_card.py /Volumes/<CARD NAME> ~/DashcamNew --min-counter 8704     # only clips newer than #8704
Result: <dest>/YYYY-MM-DD/Normal|Parking|Event/<name>.MP4. Files already in <dest> with the same size are skipped,
so it is safe to run after a partial WiFi copy, and to stop and re-run. Only .MP4 clips are copied (not the hidden
low-resolution .s_ copies or the .THM/.RSD helper files).
"""
import argparse, os, re, shutil, sys, time
ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("card"); ap.add_argument("dest")
ap.add_argument("--min-counter", type=int, default=0, help="only copy clips whose counter is greater than this (e.g. 8704)")
a = ap.parse_args()
card, dest = a.card, a.dest
pat = re.compile(r"^(NO|PA|EV)(\d{8})-\d{6}-(\d{6})[FR]\.MP4$")
folder = {"NO": "Normal", "PA": "Parking", "EV": "Event"}
items = []
for root, dirs, files in os.walk(card):
    dirs[:] = [d for d in dirs if not d.startswith(".")]          # skip .s_Front etc and macOS junk
    for n in files:
        m = pat.match(n)
        if m and int(m.group(3)) > a.min_counter:
            p = os.path.join(root, n)
            items.append((m.group(1), m.group(2), n, p, os.path.getsize(p)))
if not items: sys.exit(f"no dashcam clips found under {card} - check the path")
items.sort(key=lambda i: (["EV", "PA", "NO"].index(i[0]), i[2]), reverse=False)
total = sum(i[4] for i in items); done = 0; start = time.time(); copied = skipped = 0
print(f"{len(items)} clips, {total/1e9:.1f} GB on the card")
for pre, day, name, src, size in items:
    d = os.path.join(dest, f"{day[:4]}-{day[4:6]}-{day[6:]}", folder[pre]); os.makedirs(d, exist_ok=True)
    out = os.path.join(d, name)
    if os.path.exists(out) and os.path.getsize(out) == size:
        skipped += 1; done += size; continue
    shutil.copyfile(src, out + ".part"); os.replace(out + ".part", out)
    copied += 1; done += size
    el = time.time() - start
    print(f"{done/1e9:6.1f}/{total/1e9:.1f} GB  {done/el/1e6:5.1f} MB/s  {name}", flush=True)
print(f"finished: {copied} copied, {skipped} already there")

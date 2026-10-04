import http.server, re, datetime as dt, os
T=dt.datetime
now=T.now().replace(microsecond=0)
clips={}  # path -> bytes
def add(prefix,t,c,size,rear=True):
    for cam,d in (("F","Front"),("R","Rear")):
        if cam=="R" and not rear: continue
        f={"NO":"Normal","PA":"Parking","EV":"Event"}[prefix]
        clips[f"/mnt/sd/{f}/{d}/{prefix}{t:%Y%m%d-%H%M%S}-{c:06d}{cam}.MP4"]=os.urandom(size)
base=now-dt.timedelta(hours=1, minutes=30)
c=100; t=base
add("NO",t,c,50000)                         # seed clip
for i in range(3):                           # normal 60s clips
    c+=1; t+=dt.timedelta(seconds=60+(i%2)*(-1)); add("NO",t,c,300000)
c+=1; t+=dt.timedelta(seconds=196); add("PA",t,c,200000)       # parking clip 196 s later
c+=1; t+=dt.timedelta(minutes=50); add("NO",t,c,300000)       # dashcam was off 50 min, then boots
for i in range(2):
    c+=1; t+=dt.timedelta(seconds=60); add("NO",t,c,300000,rear=False)
print("seed", [k for k in clips if "000100F" in k]); print("last",c,t,flush=True)
import sys
if "--gps" in sys.argv:
    L=["$V02"]
    for k in sorted(clips):
        m=re.search(r"/(NO|EV)(\d{8}-\d{6})-(\d{6})F\.MP4",k)
        if m:
            cn = m.group(3)
            moving = "--move" in sys.argv and cn in sys.argv[sys.argv.index("--move") + 1].split(",")
            for sec in range(3):
                L.append(f"1790000000,A,{51.9 + (sec * 0.0005 if moving else 0):.6f},-2.1,0,0,0,0,0,{k.split('/')[-1]},0,0,0")
    clips["/mnt/sd/GPSData000001.txt"]=("\n".join(L)+"\n").encode()
STALL = sys.argv[sys.argv.index("--stall") + 1] if "--stall" in sys.argv else None
class H(http.server.BaseHTTPRequestHandler):
    protocol_version="HTTP/1.0"
    def log_message(self,*a): pass
    def do(self,head):
        p=re.sub(r"^/+","/",self.path)
        d=clips.get(p)
        if d is None: self.send_response(404); self.end_headers(); return
        rng=self.headers.get("Range"); s=0
        e=len(d)-1
        if rng:
            m=re.match(r"bytes=(\d+)-(\d*)",rng); s=int(m.group(1)); e=int(m.group(2)) if m.group(2) else e
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length",str(e+1-s)); self.end_headers()
        if not head:
            if STALL and STALL in p and self.command == "GET":     # --stall NAME: send a little, then trickle forever
                import time
                try:
                    self.wfile.write(d[s:s+1000]); self.wfile.flush()
                    while True:
                        self.wfile.write(b"x"); self.wfile.flush(); time.sleep(1)
                except OSError:
                    return
            self.wfile.write(d[s:e+1])
    def do_HEAD(self): self.do(True)
    def do_GET(self): self.do(False)
http.server.ThreadingHTTPServer(("127.0.0.1",8099),H).serve_forever()

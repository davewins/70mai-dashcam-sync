#!/usr/bin/env python3
import sys,os,shlex,shutil
root=os.path.abspath("nas")
for line in sys.stdin.read().splitlines():
    a=shlex.split(line.lstrip("-"))
    if not a: continue
    if a[0]=="mkdir": os.makedirs(root+a[1],exist_ok=True)
    elif a[0]=="ls":
        p=root+a[-1]
        if os.path.isdir(p):
            for n in sorted(os.listdir(p)): print(n)
        else: sys.stderr.write("not found\n")
    elif a[0]=="put": shutil.copy(a[1],root+a[2])
    elif a[0]=="rename": os.replace(root+a[1],root+a[2])

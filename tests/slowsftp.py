#!/usr/bin/env python3
import time,subprocess,sys,os
time.sleep(float(os.environ.get("PUSH_DELAY","1.5")))
sys.exit(subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),"fakesftp.py")]+sys.argv[1:],stdin=sys.stdin).returncode)

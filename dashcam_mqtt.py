#!/usr/bin/env python3
"""dashcam_mqtt.py - tiny MQTT 3.1.1 publisher (standard library only) so the Pi can tell Home Assistant what it is doing.

Settings come from the environment (/etc/default/dashcam-sync): MQTT_HOST (unset = MQTT off), MQTT_PORT (1883),
MQTT_USER, MQTT_PASS, MQTT_PREFIX (default 'dashcam'). Publishing never raises: a broker outage must not break a sync.

CLI:  dashcam_mqtt.py TOPIC_SUFFIX PAYLOAD [--retain]      e.g.  dashcam_mqtt.py hotspot up --retain
"""
import os, socket, struct, sys, time

def _s(x):
    b = x.encode()
    return struct.pack(">H", len(b)) + b

def _varlen(n):
    out = b""
    while True:
        d, n = n % 128, n // 128
        out += bytes([d | (128 if n else 0)])
        if not n: return out

def _read(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c: raise OSError("broker closed the connection")
        buf += c
    return buf

def publish_many(items, host=None, port=None, user=None, password=None, timeout=5):
    """items = [(topic, payload, retain)]. One short connection, QoS 1 so we know the broker has them. True on success."""
    host = host or os.environ.get("MQTT_HOST")
    if not host: return False
    port = int(port or os.environ.get("MQTT_PORT", 1883))
    user = user or os.environ.get("MQTT_USER"); password = password or os.environ.get("MQTT_PASS")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            flags = 0x02 | (0x80 if user else 0) | (0x40 if user and password else 0)
            body = _s("MQTT") + bytes([4, flags]) + struct.pack(">H", 60) + _s(f"dashcam-pi-{os.getpid()}")
            if user: body += _s(user)
            if user and password: body += _s(password)
            s.sendall(b"\x10" + _varlen(len(body)) + body)
            ack = _read(s, 4)
            if ack[0] != 0x20 or ack[3] != 0: raise OSError(f"MQTT connect refused (code {ack[3]})")
            for i, (topic, payload, retain) in enumerate(items, 1):
                pl = payload if isinstance(payload, bytes) else str(payload).encode()
                body = _s(topic) + struct.pack(">H", i) + pl
                s.sendall(bytes([0x32 | (1 if retain else 0)]) + _varlen(len(body)) + body)
                a = _read(s, 4)
                if a[0] != 0x40: raise OSError("no PUBACK")
            s.sendall(b"\xe0\x00")
        return True
    except (OSError, ValueError) as e:
        print(f"{time.strftime('%F %T')} mqtt publish failed: {e}", file=sys.stderr)
        return False

def publish(suffix, payload, retain=False):
    prefix = os.environ.get("MQTT_PREFIX", "dashcam")
    return publish_many([(f"{prefix}/{suffix}", payload, retain)])

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--retain"]
    if len(args) != 2: sys.exit(__doc__)
    sys.exit(0 if publish(args[0], args[1], "--retain" in sys.argv) or not os.environ.get("MQTT_HOST") else 1)

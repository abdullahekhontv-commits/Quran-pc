#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
শব্দভান্ডার MCQ সার্ভার (Python)
- শুধু Python লাগে (3.8+), আলাদা কিছু ইনস্টল করতে হয় না।
- index.html ব্রাউজারে দেখায়; ডেটা কম্পিউটারের data/vocab-data.json ফাইলে জমা রাখে।
- চালান:  python server.py   →  http://127.0.0.1:8001
"""
import os, re, sys, json, time, shutil, threading, webbrowser, traceback
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("VOCAB_DATA_DIR", os.path.join(BASE, "data"))
DATA_FILE = os.path.join(DATA_DIR, "vocab-data.json")
PREV_FILE = os.path.join(DATA_DIR, "vocab-data.prev.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
INDEX = os.path.join(BASE, "index.html")
HOST = os.environ.get("VOCAB_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOCAB_PORT", "8001"))
MAX_BODY = 30 * 1024 * 1024
DAILY_KEEP, HOURLY_KEEP = 30, 48

LOCK = threading.Lock()
STATE = {"version": 0, "data": None}

def now(): return datetime.now()
def log(*a): print(now().strftime("%H:%M:%S"), *a, flush=True)

def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def load_state():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for path in (DATA_FILE, PREV_FILE):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f: j = json.load(f)
                STATE["version"] = int(j.get("version", 1)); STATE["data"] = j["data"]
                if path == PREV_FILE: log("⚠ মূল ডেটা ফাইল পড়া যায়নি, আগের কপি (prev) থেকে লোড হয়েছে")
                return
            except Exception:
                log("⚠ ফাইল পড়তে সমস্যা:", path); traceback.print_exc()

def prune(prefix, keep):
    try: names = sorted(n for n in os.listdir(BACKUP_DIR) if n.startswith(prefix) and n.endswith(".json"))
    except OSError: return
    for old in names[:-keep]:
        try: os.remove(os.path.join(BACKUP_DIR, old))
        except OSError: pass

def rotate_before_commit():
    if not os.path.exists(DATA_FILE): return
    t = now()
    for name in ("vocab-%s.json" % t.date().isoformat(), "hourly-%s.json" % t.strftime("%Y%m%d-%H")):
        p = os.path.join(BACKUP_DIR, name)
        if not os.path.exists(p): shutil.copyfile(DATA_FILE, p)
    prune("vocab-", DAILY_KEEP); prune("hourly-", HOURLY_KEEP)

def commit(data):
    rotate_before_commit()
    if os.path.exists(DATA_FILE): shutil.copyfile(DATA_FILE, PREV_FILE)
    STATE["version"] += 1; STATE["data"] = data
    write_json_atomic(DATA_FILE, {"version": STATE["version"], "updated": now().isoformat(timespec="seconds"), "data": data})
    return STATE["version"]

def validate(d):
    if not isinstance(d, dict): return "ফাইলটি এই সফটওয়্যারের ডেটা বলে মনে হচ্ছে না"
    if not isinstance(d.get("words"), list) or not isinstance(d.get("results"), list): return "ফাইলে শব্দ/ফলাফলের তালিকা নেই — এটি এই সফটওয়্যারের ব্যাকআপ নয়"
    for w in d["words"]:
        if not (isinstance(w, dict) and w.get("id") and "w" in w and "m" in w): return "শব্দতালিকার গঠন সঠিক নয়"
    return None

def allowed_origins():
    return {"http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}

def inject(html):
    with LOCK:
        payload = {"version": STATE["version"], "data": json.dumps(STATE["data"], ensure_ascii=False, separators=(",", ":")) if STATE["data"] is not None else None}
    js = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    tag = "<script>window.__VOCAB_SERVER__=%s;</script>" % js
    i = html.find("<head>")
    return html[:i + 6] + "\n" + tag + html[i + 6:] if i >= 0 else tag + html

class H(BaseHTTPRequestHandler):
    server_version = "VocabServer/1.0"
    def log_message(self, *a): pass
    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        if not any(k == "Cache-Control" for k, _ in (extra or [])): self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY"); self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or []): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def _json(self, status, obj): self._send(status, json.dumps(obj, ensure_ascii=False))
    def _post_ok(self):
        if self.headers.get("X-Vocab") != "1": self._json(403, {"ok": False, "error": "অনুমতি নেই"}); return False
        o = self.headers.get("Origin")
        if o and o not in allowed_origins(): self._json(403, {"ok": False, "error": "অনুমোদিত নয় (origin)"}); return False
        return True
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY: raise ValueError("ফাইল অনেক বড়")
        return self.rfile.read(n)
    def do_GET(self):
        try:
            p = urlparse(self.path).path
            if p in ("/", "/index.html"):
                with open(INDEX, encoding="utf-8") as f: html = f.read()
                self._send(200, inject(html), "text/html; charset=utf-8")
            elif p == "/api/data":
                with LOCK: self._json(200, {"version": STATE["version"], "data": STATE["data"]})
            elif p == "/api/export":
                with LOCK: d = STATE["data"]
                if d is None: return self._json(404, {"ok": False, "error": "এখনও কোনো ডেটা নেই"})
                self._send(200, json.dumps(d, ensure_ascii=False), "application/json; charset=utf-8", [("Content-Disposition", "attachment; filename=vocab_backup_%s.json" % now().strftime("%Y%m%d-%H%M"))])
            elif p.startswith("/fonts/"):
                name = p[7:]; fp = os.path.join(BASE, "fonts", name)
                if not re.match(r"^[A-Za-z0-9._-]+\.(woff2?|ttf|otf)$", name) or not os.path.isfile(fp): return self._send(404, "পাওয়া যায়নি", "text/plain; charset=utf-8")
                with open(fp, "rb") as f: body = f.read()
                ct = {"woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf", "otf": "font/otf"}[name.rsplit(".", 1)[1]]
                self._send(200, body, ct, [("Cache-Control", "public, max-age=86400")])
            elif p == "/health": self._json(200, {"ok": True, "version": STATE["version"]})
            else: self._send(404, "পাওয়া যায়নি", "text/plain; charset=utf-8")
        except Exception:
            traceback.print_exc(); self._json(500, {"ok": False, "error": "সার্ভারে ত্রুটি"})
    def do_POST(self):
        try:
            p = urlparse(self.path).path
            if not self._post_ok(): return
            if p == "/api/data":
                j = json.loads(self._body().decode("utf-8"))
                data = json.loads(j["data"]) if isinstance(j.get("data"), str) else j.get("data")
                err = validate(data)
                if err: return self._json(400, {"ok": False, "error": err})
                with LOCK:
                    if not j.get("force") and int(j.get("baseVersion", -1)) != STATE["version"]:
                        return self._json(409, {"ok": False, "conflict": True, "version": STATE["version"]})
                    v = commit(data)
                self._json(200, {"ok": True, "version": v})
            elif p == "/api/import":
                data = json.loads(self._body().decode("utf-8"))
                if isinstance(data, dict) and isinstance(data.get("data"), dict) and "version" in data: data = data["data"]  # data ফোল্ডারের ফাইলও চলবে
                err = validate(data)
                if err: return self._json(400, {"ok": False, "error": err})
                with LOCK:
                    if os.path.exists(DATA_FILE):
                        shutil.copyfile(DATA_FILE, os.path.join(BACKUP_DIR, "before-import-%s.json" % now().strftime("%Y%m%d-%H%M%S")))
                    v = commit(data)
                log("ব্যাকআপ ইমপোর্ট হয়েছে, নতুন সংস্করণ", v)
                self._json(200, {"ok": True, "version": v, "words": len(data["words"])})
            else: self._json(404, {"ok": False})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "অনুরোধ সঠিক নয়"})
        except Exception:
            traceback.print_exc(); self._json(500, {"ok": False, "error": "সার্ভারে ত্রুটি"})

def main():
    global PORT
    if not os.path.exists(INDEX): sys.exit("index.html পাওয়া যায়নি: " + INDEX)
    load_state()
    srv = None
    for p in range(PORT, PORT + 10):   # পোর্ট ব্যস্ত থাকলে পরের পোর্টে চলবে
        try: srv = ThreadingHTTPServer((HOST, p), H); PORT = p; break
        except OSError: continue
    if not srv: sys.exit("কোনো পোর্ট খালি পাওয়া যায়নি (%d–%d)" % (PORT, PORT + 9))
    url = "http://127.0.0.1:%d" % PORT
    print("=" * 60); print(" শব্দভান্ডার MCQ চালু হয়েছে:", url)
    print(" ডেটা ফাইল:", DATA_FILE)
    print(" বন্ধ করতে এই উইন্ডো বন্ধ করুন বা Ctrl+C চাপুন"); print("=" * 60)
    if not os.environ.get("VOCAB_NOBROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nবন্ধ হচ্ছে…")

if __name__ == "__main__":
    main()

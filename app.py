#!/usr/bin/env python3
"""
Tidy Sheets - a simple app that runs on your computer.

Start it with:   python3 app.py
A page opens in your browser. Upload a vendor's inventory spreadsheet and it is
cleaned up, products under 300 in stock are rejected, each company's bans are
applied, new products are marked, and Scan on Keepa keeps only products on
Amazon at the price and ROI you want.

Your data (companies, bans, products seen, Keepa key and results) is saved in
~/TidySheetsData/tidy.db on this computer and is never uploaded to GitHub.

Updates: the app checks GitHub for a newer version when it starts (and every 30
minutes while open) and shows an Update now bar. Bump VERSION when publishing a change.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = 7
UPDATE_URL = "https://raw.githubusercontent.com/larrypro1234/tidy-sheets/main/app.py"
DATA_DIR = Path.home() / "TidySheetsData"
DB_PATH = DATA_DIR / "tidy.db"
PORTS = range(8790, 8810)


# ============================================================== STORAGE
# Every saved thing is one JSON document at a path, e.g. "vendors/baz", "bans/baz",
# "vendors/baz/seen/0", "vendors/baz/amazon/0", "settings/keepa".

DB_LOCK = threading.Lock()


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, json TEXT NOT NULL)")
    # Every product every company offered: one row per UPC, company, upload day and file (re-uploading that file that day replaces it).
    con.execute("CREATE TABLE IF NOT EXISTS offers (upc TEXT NOT NULL, upc_raw TEXT, vendor TEXT NOT NULL, vendor_name TEXT, "
                "day TEXT NOT NULL, file TEXT NOT NULL, cost REAL, qty REAL, item TEXT, description TEXT, PRIMARY KEY (upc, vendor, day, file))")
    return con


def doc_get(path):
    with DB_LOCK, db() as con:
        row = con.execute("SELECT json FROM docs WHERE path = ?", (path,)).fetchone()
    return json.loads(row[0]) if row else None


def doc_set(path, data):
    with DB_LOCK, db() as con:
        if data is None:
            con.execute("DELETE FROM docs WHERE path = ?", (path,))
        else:
            con.execute("INSERT OR REPLACE INTO docs (path, json) VALUES (?, ?)", (path, json.dumps(data)))


def collection(path):
    """Documents directly inside a collection: "vendors" lists "vendors/baz" but not "vendors/baz/seen/0"."""
    depth = path.count("/") + 1
    with DB_LOCK, db() as con:
        rows = con.execute("SELECT path, json FROM docs WHERE path LIKE ? ESCAPE '\\'",
                           (path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%",)).fetchall()
    return [{"id": p.rsplit("/", 1)[1], "data": json.loads(j)} for p, j in rows if p.count("/") == depth]


def offers_save(b):
    """Record one uploaded file's products. Rows from the same file and day are replaced, so picking a different company fixes it."""
    rows = [(r["upc"], r.get("upc_raw"), b["vendor"], b.get("vendor_name"), b["day"], b.get("file") or "", r.get("cost"), r.get("qty"),
             r.get("item"), r.get("description")) for r in b.get("rows", []) if r.get("upc")]
    with DB_LOCK, db() as con:
        con.execute("DELETE FROM offers WHERE file = ? AND day = ?", (b.get("file") or "", b["day"]))
        con.executemany("INSERT OR REPLACE INTO offers VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def offers_find(q):
    """Look up by UPC (any format, or part of one) or by words in the product name."""
    q = (q or "").strip()
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 6 and len(digits) >= len(q.replace(" ", "")) - 2:
        upc = digits.lstrip("0")
        where, args = "upc = ? OR upc LIKE ?", [upc, f"%{upc}%"]
    else:
        words = [w for w in re.split(r"\s+", q) if w][:6]
        if not words:
            return []
        where, args = " AND ".join("description LIKE ?" for _ in words), [f"%{w}%" for w in words]
    with DB_LOCK, db() as con:
        rows = con.execute(f"SELECT upc, upc_raw, vendor, vendor_name, day, file, cost, qty, item, description FROM offers "
                           f"WHERE {where} ORDER BY day DESC, vendor_name LIMIT 300", args).fetchall()
    keys = ["upc", "upc_raw", "vendor", "vendor_name", "day", "file", "cost", "qty", "item", "description"]
    return [dict(zip(keys, r)) for r in rows]


def doc_count():
    with DB_LOCK, db() as con:
        return con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]


# ============================================================== IMPORT FROM THE CHROME EXTENSION
# The earlier Chrome extension kept the same documents in Chrome's extension storage (a LevelDB folder).
# On first run we read that folder directly, so the Keepa key, bans, seen products and Keepa results carry over.

def _varint(b, pos):
    result = shift = 0
    while True:
        byte = b[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _snappy(b):
    _, pos = _varint(b, 0)
    out = bytearray()
    while pos < len(b):
        tag = b[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            ln = tag >> 2
            if ln >= 60:
                nb = ln - 59
                ln = int.from_bytes(b[pos:pos + nb], "little")
                pos += nb
            ln += 1
            out += b[pos:pos + ln]
            pos += ln
            continue
        if kind == 1:
            ln, off = ((tag >> 2) & 7) + 4, ((tag >> 5) << 8) | b[pos]
            pos += 1
        elif kind == 2:
            ln, off = (tag >> 2) + 1, int.from_bytes(b[pos:pos + 2], "little")
            pos += 2
        else:
            ln, off = (tag >> 2) + 1, int.from_bytes(b[pos:pos + 4], "little")
            pos += 4
        for _ in range(ln):
            out.append(out[-off])
    return bytes(out)


def _block_entries(data):
    """Key/value pairs of one LevelDB table block (keys are prefix-compressed)."""
    restarts = int.from_bytes(data[-4:], "little")
    end = len(data) - 4 - 4 * restarts
    pos, key = 0, b""
    while pos < end:
        shared, pos = _varint(data, pos)
        unshared, pos = _varint(data, pos)
        vlen, pos = _varint(data, pos)
        key = key[:shared] + data[pos:pos + unshared]
        pos += unshared
        yield key, data[pos:pos + vlen]
        pos += vlen


def _read_table(raw):
    def block(handle):
        off, pos = _varint(handle, 0)
        size, _ = _varint(handle, pos)
        data = raw[off:off + size]
        return _snappy(data) if raw[off + size] == 1 else data

    footer = raw[-48:]
    _, pos = _varint(footer, 0)
    _, pos = _varint(footer, pos)  # skip the metaindex handle
    index = footer[pos:]
    for _, handle in _block_entries(block(index)):
        for ikey, value in _block_entries(block(handle)):
            tag = int.from_bytes(ikey[-8:], "little")
            yield tag >> 8, ikey[:-8], (value if tag & 0xFF == 1 else None)


def _read_log(raw):
    records, rec, pos = [], b"", 0
    while pos + 7 <= len(raw):
        left = 32768 - pos % 32768
        if left < 7:
            pos += left
            continue
        ln, kind = int.from_bytes(raw[pos + 4:pos + 6], "little"), raw[pos + 6]
        payload = raw[pos + 7:pos + 7 + ln]
        pos += 7 + ln
        if kind == 1:
            records.append(payload)
        elif kind == 2:
            rec = payload
        elif kind == 3:
            rec += payload
        elif kind == 4:
            records.append(rec + payload)
        elif kind == 0:
            break
    for r in records:
        seq, count, p = int.from_bytes(r[:8], "little"), int.from_bytes(r[8:12], "little"), 12
        for i in range(count):
            kind = r[p]
            klen, p = _varint(r, p + 1)
            key = r[p:p + klen]
            p += klen
            value = None
            if kind == 1:
                vlen, p = _varint(r, p)
                value = r[p:p + vlen]
                p += vlen
            yield seq + i, key, value


def read_leveldb(folder):
    """Newest value of every key in a LevelDB folder (best effort)."""
    newest = {}
    for f in sorted(folder.iterdir()):
        try:
            raw = f.read_bytes()
            rows = _read_table(raw) if f.suffix in (".ldb", ".sst") else _read_log(raw) if f.suffix == ".log" else []
            for seq, key, value in rows:
                if key not in newest or seq >= newest[key][0]:
                    newest[key] = (seq, value)
        except Exception:
            continue
    return {k.decode("utf-8", "replace"): v for k, (s, v) in newest.items() if v is not None}


def import_from_chrome():
    """Copy the Chrome extension's saved data into the app, once. Returns a short summary or ''."""
    base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    best = None
    for folder in base.glob("*/Local Extension Settings/*"):
        try:
            data = read_leveldb(folder)
        except Exception:
            continue
        if not any(k.startswith(("vendors/", "bans/")) for k in data):
            continue
        stamp = max((f.stat().st_mtime for f in folder.iterdir()), default=0)
        if not best or stamp > best[0]:
            best = (stamp, data)
    if not best:
        return ""
    docs = 0
    found = {"key": False, "companies": 0, "bans": 0, "results": 0}
    for key, value in best[1].items():
        if not re.match(r"^(vendors|bans|settings)/", key):
            continue
        try:
            data = json.loads(value.decode("utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        doc_set(key, data)
        docs += 1
        if key == "settings/keepa":
            found["key"] = bool(data.get("key"))
        elif re.match(r"^vendors/[^/]+$", key):
            found["companies"] += 1
        elif key.startswith("bans/"):
            found["bans"] += len(data.get("words") or [])
        elif "/amazon/" in key:
            found["results"] += len(data.get("codes") or {})
    if not docs:
        return ""
    parts = [f"{found['companies']} {'company' if found['companies'] == 1 else 'companies'}", f"{found['bans']} bans",
             f"{found['results']:,} Keepa results"] + (["your Keepa key"] if found["key"] else [])
    return "Brought over from the Chrome extension: " + ", ".join(parts) + "."


# ============================================================== KEEPA

def keepa_fetch(url):
    """Call the Keepa API for the page. Only api.keepa.com is allowed."""
    if not url.startswith("https://api.keepa.com/"):
        return {"status": 400, "body": json.dumps({"error": {"message": "Only Keepa can be called"}})}
    req = urllib.request.Request(url, headers={"User-Agent": "TidySheets/%d" % VERSION, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw, enc = r.status, r.read(), r.headers.get("Content-Encoding")
    except urllib.error.HTTPError as e:
        status, raw, enc = e.code, e.read(), e.headers.get("Content-Encoding")
    except Exception as e:
        return {"status": 0, "body": json.dumps({"error": {"message": str(e)}})}
    if enc == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    return {"status": status, "body": raw.decode("utf-8", "replace")}


# ============================================================== UPDATES

def fetch_latest():
    """Return the newest app.py from GitHub if it's a higher version that compiles, else None."""
    try:
        text = subprocess.run(["curl", "-fsSL", "--max-time", "10", f"{UPDATE_URL}?t={int(time.time())}"],
                              capture_output=True, text=True, check=True).stdout
        m = re.search(r"^VERSION = (\d+)", text, re.M)
        if not m or int(m.group(1)) <= VERSION:
            return None
        compile(text, "app.py", "exec")
        return text
    except Exception:
        return None


def install_and_restart(text, open_browser=True):
    me = Path(__file__).resolve()
    tmp = me.with_name("app.py.new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, me)
    os.execv(sys.executable, [sys.executable, str(me)] + ([] if open_browser else ["--no-open"]))


STATE = {"update_available": "", "imported": ""}
UPDATE_CHECK = {"last": time.time(), "busy": False}


def check_for_update():
    UPDATE_CHECK.update(last=time.time(), busy=True)
    try:
        text = fetch_latest()
        if text:
            STATE["update_available"] = re.search(r"^VERSION = (\d+)", text, re.M).group(1)
    finally:
        UPDATE_CHECK["busy"] = False


def maybe_check_for_update():
    """Look for a new version at most every 5 minutes while the page is open."""
    if not UPDATE_CHECK["busy"] and not STATE["update_available"] and time.time() - UPDATE_CHECK["last"] > 5 * 60:
        threading.Thread(target=check_for_update, daemon=True).start()


def watch_for_updates():
    while True:
        time.sleep(30 * 60)
        check_for_update()


# ============================================================== SERVER

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json"):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            self.send(PAGE.replace("__VERSION__", str(VERSION)), "text/html; charset=utf-8")
        elif url.path == "/api/doc":
            self.send(json.dumps({"data": doc_get(q.get("path", ""))}))
        elif url.path == "/api/col":
            self.send(json.dumps({"docs": collection(q.get("path", ""))}))
        elif url.path == "/api/offers":
            self.send(json.dumps({"rows": offers_find(q.get("q", ""))}))
        elif url.path == "/api/status":
            maybe_check_for_update()
            status = {"app": "tidy-sheets", "version": VERSION, "update_available": STATE["update_available"],
                      "imported": STATE["imported"]}
            STATE["imported"] = ""
            self.send(json.dumps(status))
        else:
            self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/doc":
            b = self.body()
            doc_set(b["path"], b.get("data"))
            self.send('{"ok":true}')
        elif url.path == "/api/offers":
            self.send(json.dumps({"saved": offers_save(self.body())}))
        elif url.path == "/api/keepa":
            self.send(json.dumps(keepa_fetch(self.body().get("url", ""))))
        elif url.path == "/update":
            self.send('{"ok":true}')

            def update():
                time.sleep(0.5)
                text = fetch_latest()
                if text:
                    install_and_restart(text, open_browser=False)
            threading.Thread(target=update, daemon=True).start()
        elif url.path == "/quit":
            self.send('{"ok":true}')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_error(404)


def already_running():
    """If Tidy Sheets is already open, return its address so a second double-click just shows it."""
    for port in PORTS:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.5) as r:
                if json.loads(r.read()).get("app") == "tidy-sheets":
                    return f"http://localhost:{port}"
        except Exception:
            continue
    return None


def main():
    text = fetch_latest()
    if text:
        print("Installing a newer version of the app...")
        install_and_restart(text, open_browser="--no-open" not in sys.argv)
    running = already_running()
    if running:
        print(f"Tidy Sheets is already open at {running}")
        if "--no-open" not in sys.argv:
            webbrowser.open(running)
        return
    if doc_count() == 0:
        try:
            STATE["imported"] = import_from_chrome()
        except Exception:
            STATE["imported"] = ""
    threading.Thread(target=watch_for_updates, daemon=True).start()
    for port in PORTS:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    url = f"http://localhost:{port}"
    print(f"\nTidy Sheets is running at {url}")
    print("Keep this Terminal window open. Press Control + C here to quit.\n")
    if "--no-open" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()
    print("App turned off.")


# ============================================================== PAGE

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
<title>Tidy Sheets</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
html, body { margin: 0; }
/* Layout: one upload button, then the tidied sheet. Nothing else. */
:root {
  --bg: #F3F6F7; --surface: #FFFFFF; --sunk: #EAEFF1; --ink: #17222B; --muted: #5A6772; --line: #D8E0E4;
  --accent: #1B58C9; --accent-ink: #FFFFFF; --bad: #B3261E; --good: #13795B;
  --font-display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --font-body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --font-data: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  --r: 8px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --bg: #0F1519; --surface: #172026; --sunk: #1D282F; --ink: #E4EBEF; --muted: #93A2AD; --line: #2A3740; --accent: #79A4FF; --accent-ink: #0B1622; --bad: #FF8A80; --good: #5FD3A9; color-scheme: dark; }
}
:root[data-theme="dark"] { --bg: #0F1519; --surface: #172026; --sunk: #1D282F; --ink: #E4EBEF; --muted: #93A2AD; --line: #2A3740; --accent: #79A4FF; --accent-ink: #0B1622; --bad: #FF8A80; --good: #5FD3A9; color-scheme: dark; }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
.wrap > * { min-width: 0; }
body { background: var(--bg); color: var(--ink); font-family: var(--font-body); font-size: 14px; line-height: 1.5; }
.wrap { max-width: 1200px; margin: 0 auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 24px 48px; display: grid; gap: 16px; }
.top { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px; }
.brand { display: flex; align-items: center; gap: 10px; flex: 1 1 auto; }
.brand h1 { font-family: var(--font-display); font-size: 26px; font-weight: 800; letter-spacing: -0.02em; margin: 0; }
.logo { width: 30px; height: 30px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 2px; padding: 3px; background: var(--ink); border-radius: 6px; }
.logo i { background: var(--bg); border-radius: 1px; }
.btn { display: inline-flex; align-items: center; gap: 8px; background: var(--accent); color: var(--accent-ink); border: none; border-radius: var(--r); padding: 10px 18px; cursor: pointer; font: inherit; font-weight: 600; font-size: 15px; }
.btn:hover { filter: brightness(1.08); }
.btn svg { width: 18px; height: 18px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.drop { border: 2px dashed var(--line); border-radius: 12px; background: var(--surface); padding: 56px 16px; display: grid; justify-items: center; gap: 12px; text-align: center; }
.drop.over, .drop:hover { border-color: var(--accent); }
.drop p { margin: 0; color: var(--muted); }
.err { color: var(--bad); font-weight: 500; }
.info { color: var(--muted); font-family: var(--font-data); font-size: 12.5px; overflow-wrap: anywhere; }
.info b { color: var(--ink); font-weight: 500; }
.info .ok { color: var(--good); font-weight: 500; }
.info .rej { color: var(--bad); font-weight: 500; }
td .ok, .amz .ok { color: var(--good); }
.ver { font-family: var(--font-data); font-size: 13px; font-weight: 500; color: var(--muted); letter-spacing: 0; vertical-align: middle; }
td .rej { color: var(--bad); }
.info { display: grid; gap: 10px; font-family: var(--font-body); font-size: 14px; color: var(--ink); }
.info .note { font-size: 15px; }
.views { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.vtab { border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 5px 14px; cursor: pointer; font: inherit; font-weight: 500; color: var(--ink); }
.vtab span { font-family: var(--font-data); font-size: 12px; color: var(--muted); margin-left: 4px; }
.vtab[aria-pressed="true"] { background: var(--ink); color: var(--bg); border-color: var(--ink); }
.vtab[aria-pressed="true"] span { color: inherit; opacity: .75; }
.fname { color: var(--muted); font-family: var(--font-data); font-size: 12px; overflow-wrap: anywhere; }
.pill { display: inline-block; font-family: var(--font-data); font-size: 10.5px; font-weight: 500; letter-spacing: .06em; padding: 1px 6px; border-radius: 4px; color: var(--good); background: color-mix(in srgb, var(--good) 15%, transparent); }
.pill.bad { color: var(--bad); background: color-mix(in srgb, var(--bad) 12%, transparent); letter-spacing: 0; }
.bans { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 10px 12px; }
.bans label { font-weight: 600; }
.bans input { flex: 1 1 180px; min-width: 0; max-width: 280px; border: 1px solid var(--line); border-radius: var(--r); padding: 6px 10px; background: var(--bg); font: inherit; color: var(--ink); }
.btn.sm { padding: 6px 14px; font-size: 14px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; flex: 1 1 100%; }
.chips:empty { display: none; }
.chips .ban { display: inline-flex; align-items: center; gap: 4px; border: 1px solid color-mix(in srgb, var(--bad) 40%, var(--line)); color: var(--bad); background: color-mix(in srgb, var(--bad) 8%, transparent); border-radius: 999px; padding: 2px 4px 2px 10px; font-size: 13px; font-weight: 500; }
.chips .ban button { border: none; background: none; color: inherit; cursor: pointer; font-size: 15px; line-height: 1; padding: 2px 6px; border-radius: 999px; }
.chips .ban button:hover { background: color-mix(in srgb, var(--bad) 15%, transparent); }
.vbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.vbar label { font-weight: 600; color: var(--muted); }
.vbar select, .vbar input { border: 1px solid var(--line); border-radius: var(--r); padding: 8px 10px; background: var(--surface); font: inherit; font-size: 15px; font-weight: 600; color: var(--ink); min-width: 0; max-width: 100%; }
.vbar input { font-weight: 400; width: 180px; }
.vbar form { display: flex; flex-wrap: wrap; gap: 6px; }
.btn.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
.amz { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 10px 12px; }
.amz .status { color: var(--muted); font-size: 13.5px; }
.amz form { display: flex; gap: 6px; flex-wrap: wrap; }
.amz input { border: 1px solid var(--line); border-radius: var(--r); padding: 6px 10px; background: var(--bg); font: inherit; color: var(--ink); width: 240px; max-width: 100%; }
.btn[disabled] { opacity: .5; cursor: default; }
td a { color: var(--accent); }
.banner-upd, .banner-imp { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; justify-content: space-between; border-radius: var(--r); padding: 10px 14px; }
.banner-upd { background: color-mix(in srgb, var(--accent) 12%, var(--surface)); border: 1px solid var(--accent); }
.banner-imp { background: color-mix(in srgb, var(--good) 10%, var(--surface)); border: 1px solid color-mix(in srgb, var(--good) 40%, var(--line)); }
.rowbtn { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 6px; padding: 2px 8px; font: inherit; font-size: 12px; font-weight: 500; cursor: pointer; white-space: nowrap; }
.rowbtn.rej { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 35%, var(--line)); }
.rowbtn:hover { background: var(--sunk); }
.hist { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 12px; display: grid; gap: 10px; }
.hist form { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center; }
.hist label { font-weight: 600; }
.hist input { flex: 1 1 220px; min-width: 0; max-width: 360px; border: 1px solid var(--line); border-radius: var(--r); padding: 7px 10px; background: var(--bg); font: inherit; color: var(--ink); }
.hist .note { margin: 0; color: var(--muted); }
.hist .tbl { overflow: auto; max-height: 50vh; border: 1px solid var(--line); border-radius: var(--r); }
.link { background: none; border: none; padding: 0; margin-left: 6px; color: var(--accent); font: inherit; text-decoration: underline; cursor: pointer; }
.tabs { display: flex; gap: 4px; overflow-x: auto; }
.tab { border: 1px solid var(--line); border-bottom: none; background: var(--sunk); padding: 6px 12px; border-radius: 6px 6px 0 0; cursor: pointer; font-family: var(--font-data); font-size: 12.5px; white-space: nowrap; color: var(--ink); }
.tab[aria-selected="true"] { background: var(--surface); font-weight: 500; }
.sheet { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); overflow: auto; max-height: 75vh; }
table { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 13px; }
th, td { border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: top; }
thead th { position: sticky; top: 0; background: var(--sunk); font-weight: 600; white-space: nowrap; z-index: 1; }
tfoot td { font-weight: 600; background: var(--sunk); }
td { font-variant-numeric: tabular-nums; max-width: 320px; overflow-wrap: anywhere; }
td.n { text-align: right; font-family: var(--font-data); font-size: 12.5px; white-space: nowrap; }
td.w { min-width: 240px; }
td.nw { white-space: nowrap; }
td.d { font-family: var(--font-data); font-size: 12.5px; white-space: nowrap; }
.more { padding: 12px; text-align: center; color: var(--muted); font-size: 12.5px; }
</style>

<div class="wrap">
  <header class="top">
    <div class="brand"><div class="logo" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div><h1>Tidy Sheets <span class="ver" id="ver"></span></h1></div>
    <div class="vbar" id="vendorBar" hidden>
      <label for="vendorSel">Company</label>
      <select id="vendorSel"></select>
      <form id="newVendor" hidden><input id="newName" placeholder="Company name" aria-label="Company name" autocomplete="off"><button class="btn sm" id="newSave" type="submit">Save company</button><button class="btn sm ghost" id="newCancel" type="button">Cancel</button></form>
    </div>
    <button class="btn ghost sm" id="histBtn" type="button" aria-expanded="false">History</button>
    <button class="btn ghost sm" id="quit" type="button">Quit app</button>
    <label class="btn" for="file" id="topBtn" hidden>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></svg>
      Upload another
    </label>
  </header>
  <div class="banner-upd" id="update" hidden><b>A new version of Tidy Sheets is ready.</b><button class="btn sm" id="updbtn" type="button">Update now</button></div>
  <div class="banner-imp" id="imported" hidden></div>
  <section class="hist" id="historyPanel" hidden>
    <form id="histForm">
      <label for="histQ">Look up a UPC or product</label>
      <input id="histQ" placeholder="e.g. 071444299565 or play-doh" autocomplete="off">
      <button class="btn sm" type="submit">Search</button>
      <button class="btn sm ghost" id="histClose" type="button">Close</button>
    </form>
    <div id="histOut"><p class="note">Every product in every file you upload is recorded here: which company offered it, when, our cost and qty.</p></div>
  </section>
  <input type="file" id="file" accept=".xlsx,.xlsm,.xls,.ods,.csv,.tsv,.txt,.numbers" hidden>

  <label class="drop" id="drop" for="file">
    <span class="btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></svg>
      Upload spreadsheet
    </span>
    <p id="dropText">Excel, CSV or ODS. You'll get it back cleaned up and sorted, and any product with less than 300 in stock is rejected. Products you haven't seen before from that vendor are marked new.</p>
  </label>

  <div class="info" id="info" hidden></div>
  <form class="bans" id="bans" hidden>
    <label for="banIn" id="banLbl">Ban a word or brand</label>
    <input id="banIn" placeholder="e.g. balloon or PartyLite" autocomplete="off">
    <button class="btn sm" type="submit">Ban</button>
    <div class="chips" id="chips"></div>
  </form>
  <div class="amz" id="amz" hidden>
    <b>Amazon check</b>
    <button class="btn sm" id="scanBtn" type="button">Scan on Keepa</button>
    <button class="btn sm ghost" id="stopBtn" type="button" hidden>Stop</button>
    <span id="amzStatus" class="status" role="status"></span>
    <form id="keyForm" hidden><input id="keyIn" type="password" placeholder="Keepa API key" aria-label="Keepa API key" autocomplete="off"><button class="btn sm" type="submit">Save key</button></form>
    <button class="link" id="keyChange" type="button" hidden>Change Keepa key</button>
  </div>
  <div id="result" hidden>
    <div class="tabs" id="tabs" role="tablist"></div>
    <div class="sheet" id="sheet"></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const letter = i => { let s = ""; i++; while (i > 0) { const m = (i - 1) % 26; s = String.fromCharCode(65 + m) + s; i = Math.floor((i - 1) / 26); } return s; };
const isoD = d => d.toISOString().slice(0, 10);
const plural = (n, one, many) => n + " " + (n === 1 ? one : (many || one + "s"));
const NUMERIC = ["number", "currency", "percent"];
const TYPE_LABEL = { number: "123", currency: "$", percent: "%", date: "date", text: "abc" };
const PLACEHOLDER = /^(n\/?a|null|-+|—|–|\?|#n\/a|#value!|#ref!|#div\/0!|#name\?)$/i;
const TOTAL_RE = /^(grand\s+)?(sub)?totals?:?$/i;
const MONTHS = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };

/* ---------- value parsing ---------- */
function parseNum(s) {
  if (typeof s !== "string") return null;
  let t = s.replace(/\s/g, ""), neg = false, cur = "", pct = false;
  if (/^\(.*\)$/.test(t)) { neg = true; t = t.slice(1, -1); }
  if (/^[-−+]/.test(t)) { neg = neg !== (t[0] !== "+"); t = t.slice(1); }
  if (/^[$€£¥]/.test(t)) { cur = t[0]; t = t.slice(1); }
  if (/^[-−]/.test(t)) { neg = !neg; t = t.slice(1); }
  if (/%$/.test(t)) { pct = true; t = t.slice(0, -1); }
  if (!cur && /[$€£¥]$/.test(t)) { cur = t.slice(-1); t = t.slice(0, -1); }
  if (!t || t === "." || !/^(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?$/.test(t)) return null;
  if (/^0\d/.test(t) && !t.includes(".")) return null; // leading zeros: a code, not a number
  let n = parseFloat(t.replace(/,/g, ""));
  if (neg) n = -n;
  if (pct) n = n / 100;
  return { n, cur, pct };
}
function mkDate(y, m, d) {
  if (y < 100) y += y < 70 ? 2000 : 1900;
  if (m < 0 || m > 11 || d < 1 || d > 31 || y < 1000 || y > 9999) return null;
  const dt = new Date(Date.UTC(y, m, d));
  return dt.getUTCMonth() === m ? dt : null;
}
function parseDate(s, dmy) {
  if (typeof s !== "string") return null;
  let m;
  if ((m = s.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T]\d{1,2}:\d{2}.*)?$/))) return mkDate(+m[1], +m[2] - 1, +m[3]);
  if ((m = s.match(/^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})$/))) return dmy ? mkDate(+m[3], +m[2] - 1, +m[1]) : mkDate(+m[3], +m[1] - 1, +m[2]);
  if ((m = s.match(/^(?:[A-Za-z]{3,9},?\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$/))) {
    const mo = MONTHS[m[1].slice(0, 3).toLowerCase()]; return mo == null ? null : mkDate(+m[3], mo, +m[2]);
  }
  if ((m = s.match(/^(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3,9})\.?,?[\s-]+(\d{2}|\d{4})$/))) {
    const mo = MONTHS[m[2].slice(0, 3).toLowerCase()]; return mo == null ? null : mkDate(+m[3], mo, +m[1]);
  }
  return null;
}
function fmt(v, col) {
  if (v == null) return "";
  if (v instanceof Date) return isoD(v);
  if (typeof v === "number") {
    if (col.type === "currency") {
      const a = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      return (v < 0 ? "-" : "") + (col.sym || "$") + a;
    }
    if (col.type === "percent") return +(v * 100).toFixed(2) + "%";
    return col.plain ? String(v) : v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  }
  return String(v);
}
function mode(arr) { const c = {}; let best = arr[0], bc = 0; for (const a of arr) { c[a] = (c[a] || 0) + 1; if (c[a] > bc) { bc = c[a]; best = a; } } return best; }

/* ---------- the tidy pipeline ---------- */
function findHeader(rows) {
  const ne = r => r.filter(v => v != null && v.trim() !== "");
  const maxW = Math.max(0, ...rows.slice(0, 60).map(r => ne(r).length));
  for (let i = 0; i < Math.min(rows.length, 15); i++) {
    const cells = ne(rows[i]).map(v => v.trim());
    if (cells.length < Math.max(2, Math.ceil(maxW * 0.5))) continue;
    if (cells.every(v => !parseNum(v) && !parseDate(v, false))) return i;
  }
  return -1;
}

function organize(aoa, o) {
  const rep = { trim: 0, ph: 0, nums: 0, dates: 0, cased: 0, dupes: 0, blank: 0, cols: 0, title: 0, totals: 0 };
  const blank = v => v == null || v.trim() === "";
  let rows = aoa.map((r, i) => ({ src: i + 1, orig: (Array.isArray(r) ? r : []).map(v => v == null ? null : v instanceof Date ? isoD(v) : String(v)) }));
  const W = Math.max(0, ...rows.map(r => r.orig.length));
  rows.forEach(r => { while (r.orig.length < W) r.orig.push(null); });

  const hi = o.header ? findHeader(rows.map(r => r.orig)) : -1;
  const above = rows.slice(0, Math.max(hi, 0));
  rep.title = above.filter(r => !r.orig.every(blank)).length;
  rep.headerSrc = hi >= 0 ? rows[hi].src : null;
  const headRow = hi >= 0 ? rows[hi].orig : null;
  let body = rows.slice(hi + 1);

  let keep = [...Array(W).keys()];
  if (o.blanks) {
    const n = body.length;
    body = body.filter(r => !r.orig.every(blank));
    rep.blank = n - body.length + (above.length - rep.title);
    keep = keep.filter(c => !(blank(headRow ? headRow[c] : null) && body.every(r => blank(r.orig[c]))));
    rep.cols = W - keep.length;
  }

  body.forEach(r => {
    r.o = keep.map(c => r.orig[c]);
    r.val = r.o.map(v => {
      if (v == null) return null;
      if (o.trim) {
        const t = v.replace(/[\s ]+/g, " ").trim();
        if (t !== v) rep.trim++;
        v = t;
        if (PLACEHOLDER.test(v)) { rep.ph++; return null; }
      }
      return v === "" ? null : v;
    });
  });

  const seen = {};
  const cols = keep.map((c, j) => {
    let h = headRow && headRow[c] ? headRow[c].replace(/\s+/g, " ").trim() : "";
    if (!h) h = "Column " + letter(j);
    const base = h; let k = 2;
    while (seen[h.toLowerCase()]) h = base + " (" + (k++) + ")";
    seen[h.toLowerCase()] = 1;
    return { name: h, letter: letter(j), type: "text" };
  });

  let totalRows = [];
  if (o.totals) {
    const isTot = r => r.val.filter(v => v != null).slice(0, 2).some(v => typeof v === "string" && TOTAL_RE.test(v));
    totalRows = body.filter(isTot);
    body = body.filter(r => !isTot(r));
    rep.totals = totalRows.length;
  }

  if (o.types) cols.forEach((col, j) => {
    if (/\b(upc|sku|ean|gtin|barcode|isbn|zip|phone)\b/i.test(col.name)) return; // codes stay exactly as written
    const vals = body.map(r => r.val[j]).filter(v => v != null);
    if (!vals.length) return;
    let dmy = false;
    for (const v of vals) { const m = v.match(/^(\d{1,2})[-/.](\d{1,2})[-/.]\d{2,4}$/); if (m && +m[1] > 12) { dmy = true; break; } }
    const nd = vals.filter(v => parseDate(v, dmy)).length;
    const nums = vals.map(parseNum).filter(Boolean);
    if (nd / vals.length >= 0.8) {
      col.type = "date";
      body.forEach(r => { const v = r.val[j]; const d = parseDate(v, dmy); if (d) { r.val[j] = d; if (isoD(d) !== v) rep.dates++; } });
    } else if (nums.length / vals.length >= 0.8) {
      const curs = nums.filter(p => p.cur);
      if (curs.length >= nums.length / 2) { col.type = "currency"; col.sym = mode(curs.map(p => p.cur)); }
      else if (nums.filter(p => p.pct).length >= nums.length / 2) col.type = "percent";
      else col.type = "number";
      col.plain = /\b(id|code|zip|postcode|year|no|num|number|phone|ref)\b|#/i.test(col.name);
      body.forEach(r => {
        const v = r.val[j]; const p = parseNum(v); if (!p) return;
        let n = p.n;
        if (col.type === "percent" && !p.pct && Math.abs(n) > 1) n = n / 100;
        r.val[j] = n;
        if (fmt(n, col) !== v) rep.nums++;
      });
    }
  });

  if (o.case) cols.forEach((col, j) => {
    const groups = new Map();
    body.forEach(r => { const v = r.val[j]; if (typeof v !== "string") return; const k = v.toLowerCase(); if (!groups.has(k)) groups.set(k, new Map()); const m = groups.get(k); m.set(v, (m.get(v) || 0) + 1); });
    const pick = new Map();
    groups.forEach((m, k) => {
      if (m.size < 2) return;
      let best = null, bs = -1;
      m.forEach((c, v) => { const nice = /^[A-Z0-9]/.test(v) && v !== v.toUpperCase() ? 5 : 0; if (c * 10 + nice > bs) { bs = c * 10 + nice; best = v; } });
      pick.set(k, best);
    });
    if (!pick.size) return;
    body.forEach(r => { const v = r.val[j]; if (typeof v !== "string") return; const b = pick.get(v.toLowerCase()); if (b && b !== v) { if (!rep.caseEx) rep.caseEx = [v, b]; r.val[j] = b; rep.cased++; } });
  });

  if (o.dupes) {
    const s = new Set(), n = body.length;
    body = body.filter(r => { const k = JSON.stringify(r.val.map(v => v instanceof Date ? +v : v)); if (s.has(k)) return false; s.add(k); return true; });
    rep.dupes = n - body.length;
  }

  body.forEach(r => { r.ch = r.val.map((v, j) => fmt(v, cols[j]) !== (r.o[j] == null ? "" : r.o[j])); });

  let defaultSort = [];
  if (o.sort) {
    const dj = cols.findIndex(c => c.type === "date");
    if (dj >= 0) defaultSort = [{ col: dj, dir: 1 }];
    else if (cols.length && cols[0].type === "text") defaultSort = [{ col: 0, dir: 1 }];
  }
  return { cols, rows: body, rep, totalRows, rawRows: aoa.length, defaultSort };
}

const OPTS = { header: true, trim: true, blanks: true, types: true, case: true, dupes: true, totals: true, sort: true };
const MIN_QTY = 300, CHUNK = 4000;
const n = x => x.toLocaleString("en-US");
// Everything is saved by the app on this Mac (~/TidySheetsData/tidy.db), keyed by path ("vendors/baz", "bans/baz", ...).
async function api(path, body) {
  const r = await fetch(path, body === undefined ? { cache: "no-store" } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw { code: "app_" + r.status };
  return r.json();
}
function makeDb() {
  const snap = (path, v) => ({ id: path.split("/").pop(), exists: v != null, data: () => v == null ? undefined : JSON.parse(JSON.stringify(v)) });
  const docRef = path => ({
    path,
    get: async () => snap(path, (await api("/api/doc?path=" + encodeURIComponent(path))).data),
    set: async d => api("/api/doc", { path, data: d }),
    delete: async () => api("/api/doc", { path, data: null }),
    collection: c => colRef(path + "/" + c),
  });
  const colRef = path => ({
    path,
    doc: id => docRef(path + "/" + id),
    get: async () => {
      const docs = (await api("/api/col?path=" + encodeURIComponent(path))).docs.map(d => snap(path + "/" + d.id, d.data));
      return { docs, size: docs.length, empty: !docs.length };
    },
  });
  return { doc: docRef, collection: colRef };
}
const dbP = Promise.resolve(makeDb());
let sheets = [], active = 0, fileName = "", prefix = "", cur = null, hist = null, view = "kept", uploadDay = "";

/* ---------- companies ---------- */
// Each company is vendors/<key> {name, prefixes: [file-name starts that mean this company], ...}.
let vendors = [], vendor = null, lastWrite = null;
const slug = s => String(s).toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "company";
const vkey = () => vendor.key;
const GENERIC = /^(inventory|inv|deals?|offers?|list|prices?|pricing|products?|export|report|sheet|data|copy|new|file|untitled|book|stock|catalog|items?|download|spreadsheet|workbook)$/i;
function filePrefix(name) {
  const first = name.replace(/\.[^.]+$/, "").split(/[-_\s]+/).find(Boolean) || "";
  return /[a-z]/i.test(first) && !GENERIC.test(first) ? first.toUpperCase() : "";
}
async function loadVendors() {
  const db = await dbP; if (!db) return;
  try {
    const s = await db.collection("vendors").get();
    vendors = s.docs.map(d => { const x = d.data() || {}; return { key: d.id, name: typeof x.name === "string" ? x.name : d.id, prefixes: Array.isArray(x.prefixes) ? x.prefixes : [] }; });
  } catch (e) { /* keep whatever list we had */ }
}
function renderVendorBar(mode) {
  const sel = $("#vendorSel");
  const list = vendors.slice().sort((a, b) => a.name.localeCompare(b.name));
  sel.innerHTML = (vendor ? "" : `<option value="" selected disabled>Choose the company…</option>`)
    + list.map(v => `<option value="${esc(v.key)}" ${vendor && v.key === vendor.key ? "selected" : ""}>${esc(v.name)}</option>`).join("")
    + (vendor && !list.some(v => v.key === vendor.key) ? `<option value="${esc(vendor.key)}" selected>${esc(vendor.name)}</option>` : "")
    + `<option value="__new">+ New company…</option>`
    + (vendor && list.some(v => v.key === vendor.key) ? `<option value="__rename">Rename ${esc(vendor.name)}…</option>` : "");
  $("#vendorBar").hidden = !cur;
  $("#newVendor").hidden = !mode;
  if (mode) {
    $("#newVendor").dataset.mode = mode;
    $("#newName").value = mode === "rename" ? vendor.name : (prefix ? prefix.charAt(0) + prefix.slice(1).toLowerCase() : "");
    $("#newSave").textContent = mode === "rename" ? "Save name" : "Save company";
    $("#newName").focus(); $("#newName").select();
  }
}
// Put back what the last upload wrote, when the person says the file belongs to a different company.
async function undoLast() {
  const w = lastWrite; lastWrite = null;
  const db = await dbP; if (!w || !db) return;
  try {
    const vref = db.doc("vendors/" + w.key);
    for (const [id, ids] of w.before) { const ref = vref.collection("seen").doc(id); if (ids) await ref.set({ ids }); else await ref.delete(); }
    if (w.metaBefore) await vref.set(w.metaBefore); else { await vref.delete(); vendors = vendors.filter(v => v.key !== w.key); }
  } catch (e) { /* best effort */ }
}
async function useVendor(v) {
  await undoLast();
  vendor = v; view = "kept";
  await loadBans(); await loadRejects(); await loadAmazon();
  hist = { status: "loading" }; show(); renderVendorBar();
  await remember(); show(); renderVendorBar();
  rememberView(); saveSession(); recordOffers();
}

/* ---------- bans ---------- */
// Banned words and brands are kept per company, in bans/<key> {vendor, words: [...]}.
let bans = [], banSaves = Promise.resolve(), banErr = "";
// Products you rejected by hand stay hidden for that company: rejects/<key> {vendor, ids: {productId: day}}.
let rejects = new Map(), rejectSaves = Promise.resolve();
async function loadRejects() {
  rejects = new Map();
  if (!vendor) return;
  try {
    const s = await (await dbP).doc("rejects/" + vkey()).get();
    const ids = s.exists && s.data().ids;
    if (ids && typeof ids === "object") rejects = new Map(Object.entries(ids));
  } catch (e) { /* show everything */ }
}
function saveRejects() {
  const key = vkey(), name = vendor.name, ids = Object.fromEntries(rejects);
  rejectSaves = rejectSaves.then(() => dbP).then(db => db.doc("rejects/" + key).set({ vendor: name, ids }))
    .catch(() => { banErr = "Your rejected products couldn't be saved. Try again."; renderBans(); });
}
const isRejected = r => { const k = keyOf(r); return !!k && rejects.has(k); };
async function loadBans() {
  bans = []; banErr = "";
  if (!vendor) return;
  try {
    const db = await dbP; if (!db) return;
    const s = await db.doc("bans/" + vkey()).get();
    const w = s.exists && s.data().words;
    if (Array.isArray(w)) bans = w.filter(x => typeof x === "string");
  } catch (e) { banErr = `Your banned list for ${vendor.name} couldn't be loaded.`; }
}
const banRes = new Map();
const banRe = w => { if (!banRes.has(w)) banRes.set(w, new RegExp("(^|[^a-z0-9])" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i")); return banRes.get(w); };
function bannedBy(r) {
  if (!bans.length) return null;
  const text = r.val.filter(v => typeof v === "string" && !/^https?:/i.test(v)).join(" | ");
  return bans.find(w => banRe(w).test(text)) || null;
}
function saveBans() {
  const key = vkey(), name = vendor.name, words = bans.slice();
  banSaves = banSaves.then(async () => {
    const db = await dbP;
    if (!db) { banErr = "Bans work for now but can't be saved unless this page is opened in Claude while signed in."; return; }
    await db.doc("bans/" + key).set({ vendor: name, words });
    banErr = "";
  }).catch(() => { banErr = "Your banned list couldn't be saved. Try again."; }).then(renderBans);
}
function renderBans() {
  $("#chips").innerHTML = bans.map((w, i) => `<span class="ban">${esc(w)}<button type="button" data-i="${i}" aria-label="Unban ${esc(w)}">×</button></span>`).join("")
    + (banErr ? `<span class="err">${esc(banErr)}</span>` : "");
}

/* ---------- Amazon check via Keepa ---------- */
// Results are cached per company in vendors/<key>/amazon/<n> {codes: {upc: [day] | [day, asin, priceCents, rank, title]}}.
const MIN_ROI = 0.40, MIN_AMZ_PRICE = 699, MAX_RANK = 350000, MIN_SOLD = 50, EST_REF_PCT = 15, EST_FBA = 350; // ROI and price floors; fallback fees in % and cents
const AMZ_CHUNK = 1500, BATCH = 20;
let amz = { chunks: [], where: new Map(), map: new Map(), dirty: new Set() }, keepaKey = null, keyLoaded = false, scanning = null;
const normUpc = v => { const d = String(v == null ? "" : v).replace(/\D/g, "").replace(/^0+/, ""); return d.length >= 6 ? d : ""; };
function upcColumn(R) { return R.cols.findIndex(c => /\b(upc|ean|gtin|barcode)\b/i.test(c.name)); }
function amzOf(r) {
  if (cur.upj < 0) return undefined;
  const k = normUpc(r.val[cur.upj]);
  if (!k) return amz.map.size ? 0 : undefined; // no UPC: can't be matched on Amazon
  const e = amz.map.get(k);
  if (!e) return undefined;
  return e.length === 1 ? 0 : { asin: e[1], price: e[2], rank: e[3], title: e[4], ref: e.length >= 7 ? e[5] : null, fba: e.length >= 7 ? e[6] : null, sold: e.length >= 8 ? e[7] : undefined };
}
// Your cost per unit: "Low Sell" first, then other cost-like columns (never retail or comparison prices).
function costColumn(R) {
  const num = R.cols.map((c, j) => j).filter(j => ["number", "currency"].includes(R.cols[j].type));
  const find = re => num.find(j => re.test(R.cols[j].name) && !/compar|retail|msrp|amazon|list/i.test(R.cols[j].name));
  return find(/\blow ?sell\b/i) ?? find(/\bcost\b|wholesale|\bunit ?(price|cost)\b/i) ?? find(/\bprice\b/i) ?? -1;
}
// Profit and ROI for one product: Amazon price minus referral fee, FBA fee and your cost.
function econ(r) {
  const a = amzOf(r);
  if (!a || !a.price) return null;
  const price = a.price / 100, cost = cur.cj >= 0 && typeof r.val[cur.cj] === "number" ? r.val[cur.cj] : null;
  const refPct = typeof a.ref === "number" && a.ref > 0 ? a.ref : EST_REF_PCT;
  const fba = typeof a.fba === "number" && a.fba >= 0 ? a.fba / 100 : EST_FBA / 100;
  const fees = price * refPct / 100 + fba, profit = cost == null ? null : price - fees - cost;
  return { price, cost, fees, profit, roi: cost ? profit / cost : null, est: !(typeof a.ref === "number" && a.ref > 0 && typeof a.fba === "number" && a.fba >= 0) };
}
// Why an on-Amazon product doesn't make the cut, or null if it does.
function failReason(r) {
  const a = amzOf(r);
  if (!a) return null;
  if (!a.price) return "No Amazon price";
  if (a.price < MIN_AMZ_PRICE) return "Under $" + (MIN_AMZ_PRICE / 100).toFixed(2);
  if (!a.rank) return "No sales rank";
  if (a.rank > MAX_RANK) return "Rank over " + n(MAX_RANK);
  if (a.sold !== undefined && !(a.sold >= MIN_SOLD)) return `Never ${MIN_SOLD}+ sold/month`;
  const e = econ(r);
  if (e && e.roi != null && e.roi < MIN_ROI) return "ROI " + Math.round(e.roi * 100) + "%";
  return null;
}
// Best month in the past year from Amazon's "bought in past month" figure (Keepa monthlySold and its history).
const KEEPA_EPOCH_MIN = 21564000; // Keepa time is minutes since 2011-01-01
function bestMonth(p) {
  const since = Date.now() - 365 * 864e5;
  let best = typeof p.monthlySold === "number" && p.monthlySold > 0 ? p.monthlySold : 0;
  const h = Array.isArray(p.monthlySoldHistory) ? p.monthlySoldHistory : [];
  for (let i = 0; i + 1 < h.length; i += 2) if ((h[i] + KEEPA_EPOCH_MIN) * 60000 >= since && h[i + 1] > best) best = h[i + 1];
  return best;
}
async function loadAmazon() {
  amz = { chunks: [], where: new Map(), map: new Map(), dirty: new Set() };
  const db = await dbP; if (!db || !vendor) return;
  try {
    const s = await db.doc("vendors/" + vkey()).collection("amazon").get();
    amz.chunks = s.docs.map(d => { const codes = { ...((d.data() || {}).codes || {}) }; return { id: d.id, codes, size: Object.keys(codes).length }; }).sort((a, b) => +a.id - +b.id);
    amz.chunks.forEach(c => Object.entries(c.codes).forEach(([k, v]) => { if (Array.isArray(v)) { amz.map.set(k, v); amz.where.set(k, c); } }));
  } catch (e) { /* scan will refill */ }
}
function amzPut(k, entry) {
  let c = amz.where.get(k);
  if (!c) {
    c = amz.chunks[amz.chunks.length - 1];
    if (!c || c.size >= AMZ_CHUNK) { c = { id: String(amz.chunks.length), codes: {}, size: 0 }; amz.chunks.push(c); }
    c.size++; amz.where.set(k, c);
  }
  c.codes[k] = entry; amz.map.set(k, entry); amz.dirty.add(c);
}
async function saveAmazon() {
  const db = await dbP; if (!db || !vendor) return;
  const ref = db.doc("vendors/" + vkey());
  for (const c of [...amz.dirty]) { await ref.collection("amazon").doc(c.id).set({ codes: c.codes }); amz.dirty.delete(c); }
}
async function keyDoc() { return (await dbP).doc("settings/keepa"); }
async function loadKey() {
  if (keyLoaded) return keepaKey;
  try { const ref = await keyDoc(); if (ref) { const s = await ref.get(); keepaKey = s.exists && typeof s.data().key === "string" ? s.data().key : null; } } catch (e) { /* ask again */ }
  keyLoaded = true; return keepaKey;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function keepaGet(url) {
  let r;
  try { r = await api("/api/keepa", { url }); } catch (e) { throw { code: "offline" }; }
  if (!r.status) throw { code: "offline" };
  let j = null;
  try { j = JSON.parse(r.body); } catch (e) { /* not JSON */ }
  return { status: r.status, j };
}
function amzStatus(html) { $("#amzStatus").innerHTML = html; }
function renderAmzBar() {
  const bar = $("#amz");
  bar.hidden = !cur || !vendor;
  if (bar.hidden) return;
  $("#scanBtn").hidden = !!scanning; $("#stopBtn").hidden = !scanning;
  if (scanning) return;
  if (cur.upj < 0) { $("#scanBtn").disabled = true; amzStatus("No UPC column in this file, so it can't be checked on Amazon."); return; }
  $("#scanBtn").disabled = false;
  const rows = cur.kept.filter(r => !bannedBy(r) && !isRejected(r)), states = rows.map(amzOf);
  const checked = states.filter(s => s !== undefined).length, on = states.filter(s => s && s.asin).length;
  const pass = rows.filter(r => { const s = amzOf(r); return s && s.asin && !failReason(r); }).length;
  const noFees = states.filter(s => s && s.asin && s.sold === undefined).length;
  amzStatus(checked
    ? `${n(checked)} of ${n(rows.length)} checked · ${n(on)} on Amazon · <b class="ok">${n(pass)} pass: $${(MIN_AMZ_PRICE / 100).toFixed(2)}+, ${Math.round(MIN_ROI * 100)}%+ ROI, rank under ${n(MAX_RANK)}, ${MIN_SOLD}+ sold in a month</b>`
      + (noFees ? ` · press Scan to get fees and monthly sales for ${n(noFees)} products checked before` : "")
      + (cur.cj < 0 ? " · no cost column found, so ROI can't be worked out" : "")
    : `Checks each kept product's UPC on Keepa and keeps only the ones on Amazon at ${Math.round(MIN_ROI * 100)}%+ ROI.`);
  $("#keyChange").hidden = !keepaKey;
}
async function scan() {
  if (scanning || !cur || cur.upj < 0) return;
  if (!(await loadKey())) { $("#keyForm").hidden = false; $("#keyIn").focus(); amzStatus("Paste your Keepa API key once. It's saved on this Mac."); return; }
  const todo = [], seenK = new Set();
  for (const r of cur.kept) {
    if (bannedBy(r) || isRejected(r)) continue;
    const digits = String(r.val[cur.upj] ?? "").replace(/\D/g, ""), raw = digits.length === 11 ? "0" + digits : digits, k = normUpc(raw); // 11 digits: a UPC that lost its leading zero
    if (!k || seenK.has(k)) continue;
    seenK.add(k);
    const e = amz.map.get(k);
    if (!e || (e.length > 1 && e.length < 8)) todo.push({ k, raw }); // results never expire; only products never checked (or saved before fees and monthly sales were stored) are fetched
  }
  if (!todo.length) { renderAmzBar(); amzStatus(($("#amzStatus").innerHTML) + " · all up to date"); return; }
  scanning = { stop: false }; renderAmzBar();
  const today = localDay();
  let done = 0, found = 0, tokens = null;
  try {
    for (let i = 0; i < todo.length && !scanning.stop; ) {
      const batch = todo.slice(i, i + BATCH);
      amzStatus(`Scanning on Keepa… ${n(done)} of ${n(todo.length)} · ${n(found)} on Amazon${tokens != null ? ` · ${n(tokens)} tokens left` : ""}`);
      const url = `https://api.keepa.com/product?key=${encodeURIComponent(keepaKey)}&domain=1&history=1&days=365&stats=1&code=${batch.map(b => b.raw).join(",")}`;
      const { status, j } = await keepaGet(url);
      if (!j) throw { code: "unreadable", detail: "HTTP " + status };
      if (typeof j.tokensLeft === "number") tokens = j.tokensLeft;
      if (!Array.isArray(j.products)) {
        const outOfTokens = status === 429 || (typeof j.tokensLeft === "number" && j.tokensLeft <= 0);
        const detail = (j.error && (j.error.message || j.error.type)) || "HTTP " + status;
        if (!outOfTokens) throw { code: /key|auth|payment|subscription|access/i.test(detail) || status === 401 || status === 402 ? "badkey" : "keepa", detail };
        // Out of tokens: wait for Keepa to refill, then try the same batch again.
        let wait = Math.max(5000, Math.min(j.refillIn || 60000, 15 * 60000));
        while (wait > 0 && !scanning.stop) { amzStatus(`Out of Keepa tokens. Waiting ${Math.ceil(wait / 1000)}s for a refill… ${n(done)} of ${n(todo.length)} done`); await sleep(1000); wait -= 1000; }
        continue;
      }
      const hit = new Map();
      for (const p of j.products) {
        const codes = [...(p.upcList || []), ...(p.eanList || [])].map(normUpc);
        const st = p.stats || {}, curv = st.current || [];
        const price = [st.buyBoxPrice, curv[0], curv[1]].find(v => typeof v === "number" && v > 0) || 0;
        const ref = [p.referralFeePercentage, p.referralFeePercent].find(v => typeof v === "number" && v > 0) ?? null;
        const fba = p.fbaFees && typeof p.fbaFees.pickAndPackFee === "number" && p.fbaFees.pickAndPackFee >= 0 ? p.fbaFees.pickAndPackFee : null;
        const entry = [today, p.asin, price, typeof curv[3] === "number" && curv[3] > 0 ? curv[3] : 0, String(p.title || "").slice(0, 80), ref, fba, bestMonth(p)];
        for (const c of codes) if (!hit.has(c)) hit.set(c, entry);
      }
      if (batch.length === 1 && j.products.length && !hit.size) { const p = j.products[0]; hit.set(batch[0].k, [today, p.asin, 0, 0, String(p.title || "").slice(0, 80), null, null, bestMonth(p)]); }
      for (const b of batch) { const e = hit.get(b.k); amzPut(b.k, e || [today]); if (e) found++; }
      done += batch.length; i += BATCH;
      show();
      if ((i / BATCH) % 5 === 0) await saveAmazon();
    }
    await saveAmazon();
    const stopped = scanning.stop;
    scanning = null; renderAmzBar(); show();
    if (stopped) amzStatus($("#amzStatus").innerHTML + " · stopped");
  } catch (e) {
    scanning = null;
    try { await saveAmazon(); } catch (err) { /* keep going */ }
    renderAmzBar(); show();
    const msg = e && e.code === "badkey" ? `Keepa rejected the API key (${esc(e.detail)}). Use “Change Keepa key” to fix it.`
      : e && e.code === "unreadable" ? `Keepa sent back something unexpected (${esc(e.detail)}). Try Scan again.`
      : e && e.code === "offline" ? "Couldn't reach Keepa. Check your internet connection and that the Tidy Sheets app is still running, then press Scan to continue."
      : e && e.code === "keepa" ? `Keepa returned an error: ${esc(e.detail)}. Press Scan to try again.`
      : e && e.code === "quota_exceeded" ? "The app's storage is full, so results couldn't all be saved."
      : `The scan stopped (${esc((e && (e.code || e.message)) || "unknown error")}). Press Scan to continue where it left off.`;
    amzStatus(`<span class="err">${msg}</span>`);
  }
}

/* ---------- reading the sheet ---------- */
function sortRows(R) {
  const s = R.defaultSort[0];
  if (!s) return R.rows;
  return R.rows.slice().sort((x, y) => {
    const a = x.val[s.col], b = y.val[s.col];
    if (a == null || b == null) return (a == null) - (b == null);
    const na = a instanceof Date ? +a : a, nb = b instanceof Date ? +b : b;
    if (typeof na === "number" && typeof nb === "number") return na - nb;
    return String(na).localeCompare(String(nb), undefined, { numeric: true, sensitivity: "base" });
  });
}
function qtyColumn(R) {
  const num = R.cols.map((c, j) => j).filter(j => R.cols[j].type === "number");
  return num.find(j => /\b(qty|quantity)\b/i.test(R.cols[j].name)) ?? num.find(j => /\b(available|on ?hand|stock|units|inventory)\b/i.test(R.cols[j].name)) ?? -1;
}
// The column that names each product: an ID-like column whose values are (nearly) all filled and different.
function idColumn(R) {
  const ok = j => { const v = R.rows.map(r => r.val[j]).filter(x => x != null); return v.length >= R.rows.length * 0.95 && new Set(v.map(String)).size >= v.length * 0.95; };
  const js = R.cols.map((c, j) => j);
  return js.find(j => /\b(id|sku|item|product ?(no|number|#)|part)\b|#/i.test(R.cols[j].name) && ok(j))
    ?? js.find(j => /\b(upc|ean|gtin|barcode|code)\b/i.test(R.cols[j].name) && ok(j))
    ?? js.find(j => R.cols[j].type === "text" && ok(j)) ?? -1;
}
const keyOf = r => cur.idj < 0 || r.val[cur.idj] == null ? null : String(r.val[cur.idj] instanceof Date ? isoD(r.val[cur.idj]) : r.val[cur.idj]).trim().toUpperCase();
const localDay = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
const niceDate = iso => { try { return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); } catch (e) { return iso; } };

function compute() {
  const R = organize(sheets[active].aoa, OPTS);
  const all = sortRows(R), qj = qtyColumn(R);
  const low = r => !(typeof r.val[qj] === "number" && r.val[qj] >= MIN_QTY);
  cur = { R, qj, kept: qj < 0 ? all : all.filter(r => !low(r)), rejected: qj < 0 ? [] : all.filter(low) };
  cur.idj = idColumn(R);
  cur.upj = upcColumn(R);
  cur.cj = costColumn(R);
}

/* ---------- remembering products ---------- */
async function remember() {
  const db = await dbP;
  if (!db) { hist = { status: "off" }; return; }
  if (!vendor) { hist = { status: "pick" }; return; }
  if (cur.idj < 0) { hist = { status: "noid" }; return; }
  try {
    const vref = db.doc("vendors/" + vkey());
    const [vs, cs] = await Promise.all([vref.get(), vref.collection("seen").get()]);
    const meta = vs.exists ? vs.data() : null;
    const chunks = cs.docs.map(d => { const ids = { ...((d.data() || {}).ids || {}) }; return { id: d.id, ids, size: Object.keys(ids).length }; }).sort((a, b) => +a.id - +b.id);
    const seen = new Set(); chunks.forEach(c => Object.keys(c.ids).forEach(k => seen.add(k)));
    const fresh = [...new Set(cur.kept.filter(r => !bannedBy(r)).map(keyOf).filter(k => k && !seen.has(k)))];
    hist = { status: "ready", first: !meta || !meta.uploads, last: meta && meta.lastUpload, fresh: new Set(fresh) };
    view = !hist.first && fresh.length ? "new" : "kept";
    show();
    // Save what was just shown so it isn't new next time, keeping a copy so a wrong company can be undone.
    const today = localDay(), dirty = new Set(), before = new Map();
    let last = chunks[chunks.length - 1];
    for (const k of fresh) {
      if (!last || last.size >= CHUNK) { last = { id: String(chunks.length), ids: {}, size: 0 }; chunks.push(last); before.set(last.id, null); }
      if (!before.has(last.id)) before.set(last.id, { ...last.ids });
      last.ids[k] = today; last.size++; dirty.add(last);
    }
    for (const c of dirty) await vref.collection("seen").doc(c.id).set({ ids: c.ids });
    const prefixes = [...new Set([...((meta && meta.prefixes) || []), ...(prefix ? [prefix] : [])])];
    const next = { ...(meta || {}), name: vendor.name, prefixes, idColumn: cur.R.cols[cur.idj].name, uploads: ((meta && meta.uploads) || 0) + 1, lastUpload: new Date().toISOString(), lastFile: fileName, products: seen.size + fresh.length };
    await vref.set(next);
    lastWrite = { key: vkey(), before, metaBefore: meta };
    vendors = vendors.filter(v => v.key !== vkey()).concat({ key: vkey(), name: vendor.name, prefixes });
  } catch (e) {
    hist = { status: "error", code: e && e.code };
  }
}

/* ---------- page ---------- */
function show() {
  const { R, qj, rejected } = cur;
  const why = new Map(); cur.kept.forEach(r => { const w = bannedBy(r); if (w) why.set(r, w); });
  const mine = cur.kept.filter(r => !why.has(r) && isRejected(r));
  const keptAll = cur.kept.filter(r => !why.has(r) && !isRejected(r)), banned = cur.kept.filter(r => why.has(r));
  const onOrUnchecked = keptAll.filter(r => amzOf(r) !== 0), notOn = keptAll.filter(r => amzOf(r) === 0);
  const failWhy = new Map(); onOrUnchecked.forEach(r => { const w = failReason(r); if (w) failWhy.set(r, w); });
  const byRoi = rows => rows.slice().sort((x, y) => { const a = econ(x), b = econ(y); return ((b && b.roi) ?? -1e9) - ((a && a.roi) ?? -1e9); });
  const kept = byRoi(onOrUnchecked.filter(r => !failWhy.has(r))), low = byRoi(onOrUnchecked.filter(r => failWhy.has(r)));
  const isNew = r => hist && hist.status === "ready" && !hist.first && hist.fresh.has(keyOf(r));
  const freshRows = kept.filter(isNew);
  if (view === "new" && !freshRows.length) view = "kept";
  if (view === "rejected" && !rejected.length) view = "kept";
  if (view === "banned" && !banned.length) view = "kept";
  if (view === "noamz" && !notOn.length) view = "kept";
  if (view === "low" && !low.length) view = "kept";
  if (view === "mine" && !mine.length) view = "kept";
  let rows = view === "new" ? freshRows : view === "rejected" ? rejected : view === "banned" ? banned : view === "noamz" ? notOn : view === "low" ? low : view === "mine" ? mine : [...freshRows, ...kept.filter(r => !isNew(r))];
  $("#bans").hidden = !vendor;
  if (vendor) { $("#banLbl").textContent = `Ban a word or brand from ${vendor.name}`; renderBans(); }
  renderAmzBar();
  const LIMIT = 2000, shown = rows.slice(0, LIMIT);
  $("#drop").hidden = true; $("#topBtn").hidden = false; $("#result").hidden = false; $("#info").hidden = false; $("#vendorBar").hidden = false;

  const vn = vendor ? `<b>${esc(vendor.name)}</b>` : "";
  let note = "";
  if (hist && hist.status === "pick") note = `<b>Which company sent this file?</b> Choose it at the top, or add it as a new company. Its bans and the products you've already seen will be used.`;
  else if (!hist || hist.status === "loading") note = `Checking what you've already seen from ${vn}…`;
  else if (hist.status === "ready" && hist.first) note = `First file from ${vn}. Its products are saved, so next time only the new ones will be marked.`;
  else if (hist.status === "ready" && !hist.fresh.size) note = `Nothing new from ${vn} since your last upload on ${esc(niceDate(hist.last))}.`;
  else if (hist.status === "ready") note = `<b class="ok">${n(freshRows.length)} new</b> from ${vn} since your last upload on ${esc(niceDate(hist.last))}`
    + (freshRows.length !== hist.fresh.size ? ` (${n(hist.fresh.size)} new in the file, ${n(hist.fresh.size - freshRows.length)} didn't pass your rules).` : ".");
  else if (hist.status === "noid") note = `Couldn't find a product ID column, so new products can't be tracked for this file.`;
  else if (hist.status === "off") note = `New-product tracking works when this page is opened in Claude while signed in.`;
  else note = `<span class="err">Couldn't load or save what you've seen from ${vn}.</span> Upload the file again to retry.`;

  const tab = (k, label, count) => `<button class="vtab" type="button" data-view="${k}" aria-pressed="${view === k}">${label} <span>${n(count)}</span></button>`;
  $("#info").innerHTML = `<div class="note">${note}</div>`
    + `<div class="views">${freshRows.length ? tab("new", "New", freshRows.length) : ""}${tab("kept", qj < 0 ? "All products" : "Kept", kept.length)}${rejected.length ? tab("rejected", "Under " + MIN_QTY, rejected.length) : ""}${banned.length ? tab("banned", "Banned", banned.length) : ""}${low.length ? tab("low", "Didn't pass", low.length) : ""}${notOn.length ? tab("noamz", "Not on Amazon", notOn.length) : ""}${mine.length ? tab("mine", "You rejected", mine.length) : ""}`
    + `<span class="fname">${esc(fileName)}${qj < 0 ? " · no quantity column found, nothing rejected" : ` · under ${MIN_QTY} in “${esc(R.cols[qj].name)}” is rejected`}</span></div>`;

  $("#tabs").hidden = sheets.length < 2;
  $("#tabs").innerHTML = sheets.map((s, i) => `<button class="tab" role="tab" type="button" data-i="${i}" aria-selected="${i === active}">${esc(s.name)}</button>`).join("");
  if (!R.cols.length) { $("#sheet").innerHTML = `<div class="more">This sheet is empty.</div>`; return; }
  const cls = t => ["number", "currency", "percent"].includes(t) ? "n" : t === "date" ? "d" : "";
  const badge = view === "kept" && freshRows.length, reason = view === "banned" || view === "low";
  const act = cur.idj >= 0 && ["kept", "new", "low", "noamz", "mine"].includes(view);
  const actTd = r => { const k = keyOf(r); return !k ? "<td></td>" : view === "mine"
    ? `<td><button class="rowbtn" type="button" data-undo="${esc(k)}">Undo</button></td>`
    : `<td><button class="rowbtn rej" type="button" data-rej="${esc(k)}" title="Hide this product from ${esc(vendor ? vendor.name : "this company")} for good">Reject</button></td>`; };
  const reasonOf = r => view === "banned" ? why.get(r) : failWhy.get(r);
  const amzCols = (view === "kept" || view === "new" || view === "low") && rows.some(r => amzOf(r));
  const money = c => c ? "$" + (c / 100).toFixed(2) : "";
  const usd = v => v == null ? "" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
  const amzTd = r => {
    const a = amzOf(r);
    if (!a) return `<td class="more">${a === undefined ? "not checked" : ""}</td><td></td><td></td><td></td><td></td><td></td><td></td>`;
    const e = econ(r), star = e && e.est ? `<span title="Estimated fees: ${EST_REF_PCT}% + $${(EST_FBA / 100).toFixed(2)}">*</span>` : "";
    return `<td class="nw"><a href="https://www.amazon.com/dp/${esc(a.asin)}" target="_blank" rel="noopener" title="${esc(a.title)}">${esc(a.asin)}</a></td><td class="n">${money(a.price)}</td>`
      + `<td class="n">${cur.cj >= 0 && typeof r.val[cur.cj] === "number" ? usd(r.val[cur.cj]) : ""}</td>`
      + `<td class="n"${e ? ` title="Amazon fees ${usd(e.fees)}${e.est ? " (estimated)" : ""}"` : ""}>${e ? usd(e.profit) + star : ""}</td>`
      + `<td class="n"><b class="${e && e.roi != null && e.roi >= MIN_ROI ? "ok" : "rej"}">${e && e.roi != null ? Math.round(e.roi * 100) + "%" : ""}</b></td><td class="n">${a.rank ? n(a.rank) : ""}</td><td class="n">${a.sold === undefined ? "" : a.sold ? n(a.sold) + "+" : "none"}</td>`;
  };
  const shownCols = R.cols.map((c, j) => j).filter(j => j !== cur.upj && !(j === cur.idj && cur.upj >= 0));
  if (cur.upj >= 0) shownCols.unshift(cur.upj);
  const keepScroll = $("#sheet").scrollTop;
  $("#sheet").innerHTML = `<table><thead><tr>${act ? "<th></th>" : ""}${badge ? "<th></th>" : ""}${reason ? `<th>${view === "banned" ? "Banned for" : "Why"}</th>` : ""}${amzCols ? "<th>ASIN</th><th>Amazon price</th><th>Our cost</th><th>Profit</th><th>ROI</th><th>Sales rank</th><th title='Most bought in one month, past 12 months'>Best month</th>" : ""}${shownCols.map(j => `<th>${esc(R.cols[j].name)}</th>`).join("")}</tr></thead><tbody>${shown.map(r =>
    `<tr>${act ? actTd(r) : ""}${badge ? `<td>${isNew(r) ? '<span class="pill">NEW</span>' : ""}</td>` : ""}${reason ? `<td><span class="pill bad">${esc(reasonOf(r))}</span></td>` : ""}${amzCols ? amzTd(r) : ""}${shownCols.map(j => [r.val[j], j]).map(([v, j]) => `<td class="${typeof v === "string" ? (/\s/.test(v) ? (v.length > 24 ? "w" : "") : (v.length <= 20 ? "nw" : "")) : cls(R.cols[j].type)}">${esc(fmt(v, R.cols[j]))}</td>`).join("")}</tr>`).join("")}</tbody>${view === "kept" && R.totalRows.length ? `<tfoot>${R.totalRows.map(r => `<tr>${act ? "<td></td>" : ""}${badge ? "<td></td>" : ""}${amzCols ? "<td></td><td></td><td></td><td></td><td></td><td></td><td></td>" : ""}${shownCols.map(j => `<td>${esc(r.val[j] ?? "")}</td>`).join("")}</tr>`).join("")}</tfoot>` : ""}</table>${rows.length > LIMIT ? `<div class="more">Showing the first ${n(LIMIT)} of ${n(rows.length)} rows.</div>` : ""}`;
  $("#sheet").scrollTop = keepScroll;
}

async function loadFile(f) {
  if (!f) return;
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  try {
    const wb = ["csv", "tsv", "txt"].includes(ext)
      ? XLSX.read(await f.text(), { type: "string", raw: true })
      : XLSX.read(await f.arrayBuffer(), { type: "array" });
    const list = wb.SheetNames.map(s => ({ name: s, aoa: XLSX.utils.sheet_to_json(wb.Sheets[s], { header: 1, raw: false, defval: null, blankrows: true, dateNF: "yyyy-mm-dd" }) })).filter(s => s.aoa.length);
    if (!list.length) throw new Error("empty");
    sheets = list; active = 0; fileName = f.name; prefix = filePrefix(f.name); view = "kept"; uploadDay = localDay();
  } catch (e) {
    $("#drop").hidden = false; $("#result").hidden = true; $("#info").hidden = true; $("#topBtn").hidden = true; $("#vendorBar").hidden = true; $("#bans").hidden = true; $("#amz").hidden = true;
    $("#dropText").innerHTML = `<span class="err">“${esc(f.name)}” couldn't be read.</span> Try an .xlsx, .xls, .ods or .csv file that isn't password-protected.`;
    return;
  }
  lastWrite = null;
  const db = await dbP;
  await loadVendors();
  const match = prefix && vendors.find(v => v.prefixes.includes(prefix) || v.key === slug(prefix));
  vendor = match ? { key: match.key, name: match.name } : (!db ? { key: slug(prefix || "file"), name: prefix || "This file" } : null);
  await loadBans(); await loadRejects(); await loadAmazon();
  compute(); hist = vendor ? { status: "loading" } : { status: "pick" }; show(); renderVendorBar();
  if (vendor) { await remember(); show(); renderVendorBar(); }
  rememberView(); saveSession(); recordOffers();
}

/* ---------- picking up where you left off ---------- */
// The last uploaded file, its company and what was new are kept in session/last, so a refresh or an update shows the same sheet.
function saveSession() {
  const h = !hist || hist.status === "loading" ? null
    : hist.status === "ready" ? { status: "ready", first: hist.first, last: hist.last, fresh: [...hist.fresh] } : { status: hist.status };
  dbP.then(db => db.doc("session/last").set({ fileName, prefix, active, sheets, vendor, hist: h, day: uploadDay })).catch(() => { /* next upload saves again */ });
}
function rememberView() { try { localStorage.setItem("tidyView", view); } catch (e) { /* tab choice only */ } }
async function restoreSession() {
  let s = null;
  try { s = (await (await dbP).doc("session/last").get()).data(); } catch (e) { return; }
  if (!s || !Array.isArray(s.sheets) || !s.sheets.length || cur) return;
  sheets = s.sheets; active = Math.min(s.active || 0, sheets.length - 1); fileName = s.fileName || ""; prefix = s.prefix || "";
  uploadDay = s.day || localDay();
  await loadVendors();
  const v = s.vendor && vendors.find(x => x.key === s.vendor.key);
  vendor = v ? { key: v.key, name: v.name } : null;
  await loadBans(); await loadRejects(); await loadAmazon();
  if (cur) return; // a new file was uploaded meanwhile
  compute();
  hist = !vendor ? { status: "pick" }
    : s.hist && s.hist.status === "ready" ? { ...s.hist, fresh: new Set(s.hist.fresh || []) }
    : s.hist || { status: "ready", first: true, fresh: new Set() };
  view = "kept";
  try { view = localStorage.getItem("tidyView") || "kept"; } catch (e) { /* default tab */ }
  show(); renderVendorBar();
  recordOffers(); // the file that was open before History existed gets recorded too
}

/* ---------- history: where each UPC was offered ---------- */
function descColumn(R) {
  const t = R.cols.map((c, j) => j).filter(j => R.cols[j].type === "text");
  return t.find(j => /\b(desc|description|title|product|item name|name)\b/i.test(R.cols[j].name)) ?? t.find(j => j !== cur.idj && j !== cur.upj) ?? -1;
}
function recordOffers() {
  if (!vendor || !cur || cur.upj < 0) return;
  const R = cur.R, dj = descColumn(R), num = v => typeof v === "number" ? v : null, rows = [];
  for (const r of R.rows) {
    const raw = String(r.val[cur.upj] ?? "").replace(/\D/g, ""), upc = normUpc(raw);
    if (!upc) continue;
    rows.push({ upc, upc_raw: raw.length === 11 ? "0" + raw : raw, cost: cur.cj >= 0 ? num(r.val[cur.cj]) : null, qty: cur.qj >= 0 ? num(r.val[cur.qj]) : null,
      item: keyOf(r) || "", description: dj >= 0 && r.val[dj] != null ? String(r.val[dj]).slice(0, 200) : "" });
  }
  api("/api/offers", { vendor: vendor.key, vendor_name: vendor.name, day: uploadDay || localDay(), file: fileName, rows }).catch(() => { /* recorded next upload */ });
}
async function searchHistory() {
  const q = $("#histQ").value.trim(), out = $("#histOut");
  if (!q) return;
  out.innerHTML = `<p class="note">Searching…</p>`;
  let rows;
  try { rows = (await api("/api/offers?q=" + encodeURIComponent(q))).rows; } catch (e) { out.innerHTML = `<p class="err">Couldn't search. Make sure the Tidy Sheets app is still running.</p>`; return; }
  if (!rows.length) { out.innerHTML = `<p class="note">No offers found for “${esc(q)}”. Only files uploaded since this feature was added are recorded.</p>`; return; }
  const companies = new Set(rows.map(r => r.vendor_name || r.vendor)), upcs = new Set(rows.map(r => r.upc));
  const usd = v => v == null ? "" : "$" + Number(v).toFixed(2);
  out.innerHTML = `<p class="note"><b>${n(rows.length)}</b> ${rows.length === 1 ? "offer" : "offers"} from <b>${n(companies.size)}</b> ${companies.size === 1 ? "company" : "companies"}${upcs.size > 1 ? ` across ${n(upcs.size)} products` : ""}${rows.length >= 300 ? " (showing the latest 300)" : ""}.</p>`
    + `<div class="tbl"><table><thead><tr><th>Date</th><th>Company</th><th>Our cost</th><th>Qty</th><th>UPC</th><th>Product</th><th>File</th></tr></thead><tbody>`
    + rows.map(r => `<tr><td class="d">${esc(r.day)}</td><td class="nw"><b>${esc(r.vendor_name || r.vendor)}</b></td><td class="n">${usd(r.cost)}</td><td class="n">${r.qty == null ? "" : n(r.qty)}</td>`
      + `<td class="nw">${esc(r.upc_raw || r.upc)}</td><td class="w">${esc(r.description || "")}</td><td class="more" style="text-align:left">${esc(r.file || "")}</td></tr>`).join("")
    + `</tbody></table></div>`;
}

/* ---------- events ---------- */
$("#file").addEventListener("change", e => { loadFile(e.target.files[0]); e.target.value = ""; });
$("#vendorSel").addEventListener("change", e => {
  const v = e.target.value;
  if (v === "__new" || v === "__rename") return renderVendorBar(v === "__new" ? "new" : "rename");
  const found = vendors.find(x => x.key === v);
  if (found && (!vendor || found.key !== vendor.key)) useVendor({ key: found.key, name: found.name });
});
$("#newVendor").addEventListener("submit", async e => {
  e.preventDefault();
  const name = $("#newName").value.trim().replace(/\s+/g, " ").slice(0, 60);
  if (!name) return;
  if ($("#newVendor").dataset.mode === "rename") {
    vendor.name = name;
    const db = await dbP;
    try { if (db) { const ref = db.doc("vendors/" + vkey()); const s = await ref.get(); if (s.exists) await ref.set({ ...s.data(), name }); } } catch (err) { /* name still shows for this visit */ }
    vendors = vendors.map(v => v.key === vkey() ? { ...v, name } : v);
    if (lastWrite && lastWrite.key === vkey() && lastWrite.metaBefore) lastWrite.metaBefore = { ...lastWrite.metaBefore, name };
    show(); renderVendorBar();
    return;
  }
  const existing = vendors.find(v => v.name.toLowerCase() === name.toLowerCase());
  if (existing) return useVendor({ key: existing.key, name: existing.name });
  let key = slug(name), i = 2;
  while (vendors.some(v => v.key === key)) key = slug(name) + "-" + (i++);
  useVendor({ key, name });
});
$("#newCancel").addEventListener("click", () => renderVendorBar());
$("#bans").addEventListener("submit", e => {
  e.preventDefault();
  const w = $("#banIn").value.trim().replace(/\s+/g, " ");
  if (!w || !vendor) return;
  if (!bans.some(b => b.toLowerCase() === w.toLowerCase())) { bans.push(w); saveBans(); }
  $("#banIn").value = "";
  if (cur) show();
});
$("#sheet").addEventListener("click", e => {
  const b = e.target.closest("button[data-rej], button[data-undo]");
  if (!b || !vendor) return;
  if (b.dataset.rej) rejects.set(b.dataset.rej, localDay()); else rejects.delete(b.dataset.undo);
  saveRejects(); show();
});
$("#chips").addEventListener("click", e => {
  const b = e.target.closest("button[data-i]"); if (!b) return;
  bans.splice(+b.dataset.i, 1); saveBans();
  if (cur) show();
});
$("#scanBtn").addEventListener("click", scan);
$("#stopBtn").addEventListener("click", () => { if (scanning) scanning.stop = true; });
$("#keyChange").addEventListener("click", () => { $("#keyForm").hidden = false; $("#keyIn").value = ""; $("#keyIn").focus(); });
$("#keyForm").addEventListener("submit", async e => {
  e.preventDefault();
  const k = $("#keyIn").value.trim();
  if (!k) return;
  keepaKey = k; keyLoaded = true; $("#keyIn").value = ""; $("#keyForm").hidden = true;
  try { const ref = await keyDoc(); if (ref) await ref.set({ key: k }); } catch (err) { amzStatus(`<span class="err">The key couldn't be saved, so you'll be asked again next time.</span>`); }
  scan();
});
$("#info").addEventListener("click", e => { const b = e.target.closest(".vtab"); if (b) { view = b.dataset.view; rememberView(); show(); } });
$("#tabs").addEventListener("click", e => { const b = e.target.closest(".tab"); if (b) { active = +b.dataset.i; lastWrite = null; compute(); hist = vendor ? { status: "loading" } : { status: "pick" }; show(); if (vendor) remember().then(() => { show(); saveSession(); }); else saveSession(); } });
window.addEventListener("dragover", e => { e.preventDefault(); $("#drop").classList.add("over"); });
window.addEventListener("dragleave", () => $("#drop").classList.remove("over"));
window.addEventListener("drop", e => { e.preventDefault(); $("#drop").classList.remove("over"); loadFile(e.dataTransfer?.files?.[0]); });

// Version, Update now bar, and anything brought over from the Chrome extension.
$("#ver").textContent = "v__VERSION__";
async function pollStatus() {
  try {
    const s = await api("/api/status");
    $("#update").hidden = !s.update_available;
    if (s.imported) { $("#imported").textContent = s.imported; $("#imported").hidden = false; }
  } catch (e) { /* app closed */ }
  setTimeout(pollStatus, 60000);
}
pollStatus();
restoreSession();
$("#updbtn").addEventListener("click", async () => {
  $("#updbtn").disabled = true; $("#updbtn").textContent = "Updating…";
  try { await api("/update", {}); } catch (e) { /* restarting */ }
  setTimeout(() => location.reload(), 4000);
});
$("#histBtn").addEventListener("click", () => {
  const p = $("#historyPanel"); p.hidden = !p.hidden; $("#histBtn").setAttribute("aria-expanded", String(!p.hidden));
  if (!p.hidden) $("#histQ").focus();
});
$("#histClose").addEventListener("click", () => { $("#historyPanel").hidden = true; $("#histBtn").setAttribute("aria-expanded", "false"); });
$("#histForm").addEventListener("submit", e => { e.preventDefault(); searchHistory(); });
$("#quit").addEventListener("click", async () => {
  try { await api("/quit", {}); } catch (e) { /* already off */ }
  document.body.innerHTML = '<div class="wrap"><h1 style="font-family:var(--font-display)">Tidy Sheets is turned off.</h1><p>Double-click the Tidy Sheets icon on your Desktop to open it again.</p></div>';
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()

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

VERSION = 27
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

def catalog_post(url, payload):
    """Send one product to the catalog sheet's Apps Script web app. Only script.google.com is allowed."""
    if not re.match(r"^https://script\.google\.com/macros/s/[\w-]+/exec$", url or ""):
        return {"ok": False, "error": "That isn't a Google Apps Script web app link (it should end in /exec)."}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # Google answers with a redirect to the result, which urllib follows
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Google answered {e.code}. Check the web app link and that access is set to Anyone."}
    except Exception as e:
        return {"ok": False, "error": f"Couldn't reach Google ({e})."}
    try:
        return json.loads(text)
    except Exception:
        return {"ok": False, "error": "Google sent back a page instead of an answer. Check that the web app's access is set to Anyone."}


DOWNLOADS = Path.home() / "Downloads"
SHEET_TYPES = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".ods"}


def recent_downloads(days=7, limit=8):
    """Spreadsheets saved to Downloads in the last week, newest first."""
    try:
        files = [f for f in DOWNLOADS.iterdir()
                 if f.is_file() and f.suffix.lower() in SHEET_TYPES and not f.name.startswith(("~$", "."))]
    except PermissionError:
        return {"error": "no_access", "files": []}
    except FileNotFoundError:
        return {"files": []}
    since = time.time() - days * 86400
    files = sorted((f for f in files if f.stat().st_mtime >= since), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    return {"files": [{"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime} for f in files]}


def download_bytes(name):
    """One spreadsheet straight from Downloads (no folders, only spreadsheet types)."""
    f = DOWNLOADS / name
    if "/" in name or "\\" in name or name.startswith(".") or f.suffix.lower() not in SHEET_TYPES or not f.is_file():
        return None
    return f.read_bytes()


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
        elif url.path == "/api/downloads":
            self.send(json.dumps(recent_downloads()))
        elif url.path == "/api/download":
            data = download_bytes(q.get("name", ""))
            if data is None:
                return self.send_error(404)
            self.send(data, "application/octet-stream")
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
        elif url.path == "/api/catalog":
            b = self.body()
            self.send(json.dumps(catalog_post(b.get("url", ""), b.get("payload") or {})))
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
.amz form input { border: 1px solid var(--line); border-radius: var(--r); padding: 6px 10px; background: var(--bg); font: inherit; color: var(--ink); width: 240px; max-width: 100%; }
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
.pill.dup { color: var(--muted); background: var(--sunk); letter-spacing: 0; cursor: help; }
.steps-list { margin: 0; padding-left: 20px; display: grid; gap: 4px; max-width: 75ch; }
.row-gap { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
#catScript { width: 100%; min-height: 120px; font-family: var(--font-data); font-size: 12px; border: 1px solid var(--line); border-radius: var(--r); padding: 8px; background: var(--bg); color: var(--ink); }
#catUrl { flex: 1 1 320px; min-width: 0; border: 1px solid var(--line); border-radius: var(--r); padding: 7px 10px; background: var(--bg); font: inherit; color: var(--ink); }
.dlg-back { position: fixed; inset: 0; background: rgba(10, 16, 20, .45); display: grid; place-items: center; z-index: 20; padding: 16px; }
.dlg { background: var(--surface); color: var(--ink); border-radius: 12px; padding: 18px; width: min(460px, 100%); display: grid; gap: 10px; box-shadow: 0 10px 40px rgba(0, 0, 0, .25); }
.dlg-head { display: flex; gap: 12px; align-items: center; }
.dlg-head img { width: 72px; height: 72px; object-fit: contain; border: 1px solid var(--line); border-radius: 8px; background: #fff; flex: none; }
.dlg label { display: grid; grid-template-columns: 70px 1fr; align-items: center; gap: 10px; font-weight: 600; }
.dlg input { border: 1px solid var(--line); border-radius: var(--r); padding: 7px 10px; background: var(--bg); font: inherit; font-weight: 400; color: var(--ink); min-width: 0; }
.rowbtn.post { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, var(--line)); margin-left: 4px; }
.pill.posted { margin-left: 4px; letter-spacing: 0; }
.banner-dl { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; border-radius: var(--r); padding: 10px 14px; background: color-mix(in srgb, var(--accent) 10%, var(--surface)); border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--line)); }
.banner-dl .fn { font-family: var(--font-data); font-size: 13px; overflow-wrap: anywhere; }
.banner-dl .grow { flex: 1 1 auto; }
td .sub { display: block; font-size: 11px; color: var(--muted); white-space: nowrap; }
td .packbtn { background: none; border: none; padding: 0; margin-left: auto; font: inherit; font-size: 11px; color: var(--accent); cursor: pointer; text-decoration: underline dotted; }
td .warn { color: var(--bad); font-weight: 600; }
.offer-sw { display: inline-flex; align-items: center; gap: 6px; font-size: 13.5px; font-weight: 500; cursor: pointer; }
.offer-sw input { accent-color: var(--accent); width: auto; margin: 0; }
td.ph { width: 1%; padding: 4px 6px; }
td.ph img { width: 56px; height: 56px; object-fit: contain; background: #fff; border: 1px solid var(--line); border-radius: 6px; display: block; transition: transform .15s; }
td.ph img:hover { transform: scale(3); transform-origin: left center; position: relative; z-index: 5; box-shadow: 0 6px 24px rgba(0, 0, 0, .25); }
@media (prefers-reduced-motion: reduce) { td.ph img { transition: none; } }
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
  <div class="banner-dl" id="dlBar" hidden></div>
  <section class="hist" id="catSetup" hidden>
    <b>Connect your public catalog (one time)</b>
    <ol class="steps-list">
      <li>Open your catalog sheet and click <b>Extensions → Apps Script</b>.</li>
      <li>Delete everything in the code box, then click <b>Copy script</b> below and paste it in. Click the <b>save</b> icon.</li>
      <li>Click <b>Deploy → New deployment</b>. Click the gear next to "Select type" and pick <b>Web app</b>. Set <b>Execute as: Me</b> and <b>Who has access: Anyone</b>. Click <b>Deploy</b>.</li>
      <li>Google asks for permission: click <b>Authorize access</b>, pick your account, then <b>Advanced → Go to … (unsafe)</b> → <b>Allow</b>. It's your own script.</li>
      <li>Copy the <b>Web app URL</b> (it ends in /exec), paste it here and click <b>Save</b>.</li>
    </ol>
    <div class="row-gap"><button class="btn sm" id="catCopy" type="button">Copy script</button><span class="note" id="catCopyNote"></span></div>
    <textarea id="catScript" readonly hidden aria-label="Catalog script"></textarea>
    <form id="catForm" class="row-gap"><input id="catUrl" placeholder="https://script.google.com/macros/s/…/exec" aria-label="Web app URL" autocomplete="off"><button class="btn sm" type="submit">Save</button><button class="btn sm ghost" id="catClose" type="button">Close</button></form>
    <p class="note" id="catNote"></p>
  </section>
  <div class="dlg-back" id="postDlg" hidden>
    <form class="dlg" id="postForm">
      <div class="dlg-head"><img id="pImg" alt="" hidden><div><b id="pTitle"></b><div class="note" id="pInfo"></div></div></div>
      <label>Brand <input id="pBrand" autocomplete="off"></label>
      <label>Price <input id="pPrice" inputmode="decimal" placeholder="e.g. 7.50" autocomplete="off" required></label>
      <label>MOQ <input id="pMoq" inputmode="numeric" placeholder="e.g. 500" autocomplete="off"></label>
      <label>EXP <input id="pExp" value="NA" autocomplete="off"></label>
      <label>UPC <input id="pUpc" readonly></label>
      <p class="note" id="pStatus" role="status"></p>
      <div class="row-gap"><button class="btn sm" id="pGo" type="submit">Post to catalog</button><button class="btn sm ghost" id="pCancel" type="button">Cancel</button><button class="link" id="pSetup" type="button">Catalog settings</button></div>
    </form>
  </div>
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
    <label class="offer-sw" for="offerSw" title="Ignore the file's prices and show the most you can pay per unit for 40% ROI"><input type="checkbox" id="offerSw"> Offer mode</label>
    <button class="btn sm ghost" id="refreshBtn" type="button" hidden title="Check today's Amazon price again for the products in Kept">Refresh prices</button>
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
    if (/(upc|ucc|scc|itf|ean|gtin|barcode|isbn)|\b(sku|zip|phone)\b/i.test(col.name.replace(/_/g, " "))) return; // codes stay exactly as written
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
      col.plain = /\b(id|item|code|zip|postcode|year|no|num|number|phone|ref)\b|#/i.test(col.name.replace(/_/g, " "));
      body.forEach(r => {
        const v = r.val[j]; const p = parseNum(v); if (!p) return;
        let n = p.n;
        if (col.type === "percent" && !p.pct && Math.abs(n) > 1) n = n / 100;
        r.val[j] = n;
        if (fmt(n, col) !== v) rep.nums++;
      });
    } else if (/\b(qty|quantity|ava?i?l\w*|stock|on ?hand|units|inventory|oh)\b/i.test(col.name.replace(/_/g, " "))) {
      // A quantity column with notes in some cells ("5000+", "4000 FOB CA", "5000+ February"): the number counts,
      // the note is kept and shown next to Total qty.
      const lead = v => v.match(/^\s*(-?[\d,]+(?:\.\d+)?)\s*(\+)?\s*(.*)$/s);
      const ok = vals.filter(v => { const m = lead(v); return m && /\d/.test(m[1]); });
      if (ok.length / vals.length < 0.8) return;
      col.type = "number";
      body.forEach(r => {
        const v = r.val[j], m = v == null ? null : lead(v);
        if (!m) return;
        const n = parseFloat(m[1].replace(/,/g, "")), note = ((m[2] ? "+ " : "") + m[3]).trim();
        if (isNaN(n)) return;
        r.val[j] = n;
        if (note) (r.qtyNotes = r.qtyNotes || {})[j] = note;
        rep.nums++;
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
const GENERIC = /^(inventory|inv|deals?|offers?|list|prices?|pricing|pricelist|products?|export|report|sheet|data|copy|new|file|untitled|book|stock|stocklist|catalog|items?|download|spreadsheet|workbook|availability|closeouts?|specials?|weekly|daily)$/i;
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
    $("#newName").value = mode === "rename" ? vendor.name : (prefix ? (prefix.length <= 4 ? prefix : prefix.charAt(0) + prefix.slice(1).toLowerCase()) : "");
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
/* ---------- posting to the public catalog ---------- */
// The catalog sheet gets a small Apps Script web app (settings/catalog {url, secret}); posted UPCs are kept in catalog/posted.
let catalog = null, posted = {}, postRow = null;
async function loadCatalog() {
  try {
    const db = await dbP;
    const c = await db.doc("settings/catalog").get(), p = await db.doc("catalog/posted").get();
    catalog = c.exists ? c.data() : null;
    posted = (p.exists && p.data().upcs) || {};
  } catch (e) { /* not set up yet */ }
  if (!catalog || !catalog.secret) {
    catalog = { url: (catalog && catalog.url) || "", secret: (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()).replace(/-/g, "") };
    (await dbP).doc("settings/catalog").set(catalog).catch(() => {});
  }
}
const catalogReady = loadCatalog();
function catalogScript() {
  return `// Tidy Sheets → public catalog. Adds one product per request to the "Master List" sheet.
const SECRET = "${catalog.secret}";
const SHEET = "Master List";

function doPost(e) {
  const out = o => ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
  let d;
  try { d = JSON.parse(e.postData.contents); } catch (err) { return out({ ok: false, error: "Bad request" }); }
  if (d.secret !== SECRET) return out({ ok: false, error: "Wrong secret. Copy the script from Tidy Sheets again." });
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(SHEET) || ss.getSheets()[0];
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const width = sh.getLastColumn(), last = sh.getLastRow();
    const head = sh.getRange(1, 1, 1, width).getValues()[0].map(h => String(h).replace(/[^A-Za-z]/g, "").toUpperCase());
    const col = name => head.indexOf(name) + 1;
    const clean = v => String(v || "").replace(/\\D/g, "").replace(/^0+/, "");
    const upcCol = col("UPC");
    if (upcCol && last > 1) {
      const upcs = sh.getRange(2, upcCol, last - 1, 1).getDisplayValues().map(r => clean(r[0]));
      const i = upcs.indexOf(clean(d.upc));
      if (i >= 0) return out({ ok: false, duplicate: true, row: i + 2 });
    }
    const row = last + 1;
    if (last >= 2) {
      sh.getRange(last, 1, 1, width).copyTo(sh.getRange(row, 1, 1, width), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
      sh.setRowHeight(row, sh.getRowHeight(last));
    }
    const put = (name, v) => { const c = col(name); if (c && v !== undefined && v !== null && v !== "") sh.getRange(row, c).setValue(v); };
    put("BRAND", d.brand);
    if (col("PHOTO") && d.image) {
      try { sh.getRange(row, col("PHOTO")).setValue(SpreadsheetApp.newCellImage().setSourceUrl(d.image).build()); }
      catch (err) { sh.getRange(row, col("PHOTO")).setFormula('=IMAGE("' + d.image + '")'); }
    }
    put("COST", Number(d.price));
    put("MOQ", d.moq === "" ? "" : (isNaN(Number(d.moq)) ? d.moq : Number(d.moq)));
    put("EXP", d.exp);
    if (upcCol) sh.getRange(row, upcCol).setNumberFormat("@").setValue(String(d.upc));
    return out({ ok: true, row: row });
  } finally {
    lock.releaseLock();
  }
}
`;
}
async function openSetup(note) {
  await catalogReady;
  $("#catScript").value = catalogScript();
  $("#catUrl").value = catalog.url || "";
  $("#catNote").textContent = note || "";
  $("#catSetup").hidden = false;
  $("#catSetup").scrollIntoView({ behavior: "smooth", block: "start" });
}
function rowByKey(k) { return cur && cur.R.rows.find(r => keyOf(r) === k); }
async function openPost(k) {
  await catalogReady;
  if (!catalog.url) return openSetup("Connect your catalog first. After that, the Post button sends products straight to it.");
  const r = rowByKey(k);
  if (!r) return;
  postRow = r;
  const R = cur.R, dj = descColumn(R), a = amzOf(r), raw = upcDigits(r.val[cur.upj]);
  const brandCol = R.cols.findIndex(c => /\bbrand\b/i.test(cname(c)));
  $("#pTitle").textContent = dj >= 0 ? String(r.val[dj] || "") : "";
  const ec = econ(r);
  $("#pInfo").textContent = [ec && ec.offer ? "Max offer $" + ec.maxUnit.toFixed(2) + "/unit" : cur.cj >= 0 && typeof r.val[cur.cj] === "number" ? "Our cost $" + r.val[cur.cj].toFixed(2) : "", a && a.price ? "Amazon $" + (a.price / 100).toFixed(2) : "", a && a.asin ? a.asin : ""].filter(Boolean).join(" · ");
  $("#pBrand").value = brandCol >= 0 && r.val[brandCol] ? String(r.val[brandCol]) : "";
  $("#pPrice").value = ""; $("#pMoq").value = ""; $("#pExp").value = "NA";
  $("#pUpc").value = raw.length === 11 ? "0" + raw : raw;
  $("#pImg").hidden = true; $("#pImg").removeAttribute("src"); $("#pImg").dataset.url = "";
  $("#pGo").disabled = false; $("#pStatus").textContent = "";
  $("#postDlg").hidden = false;
  $("#pPrice").focus();
  // Photo and brand from Keepa (1 token).
  if (await loadKey()) {
    $("#pStatus").textContent = "Getting the photo and brand from Keepa…";
    try {
      const { j } = await keepaGet(`https://api.keepa.com/product?key=${encodeURIComponent(keepaKey)}&domain=1&history=0&code=${$("#pUpc").value}`);
      const p = j && Array.isArray(j.products) && j.products[0];
      if (postRow !== r) return;
      if (p) {
        const img = (Array.isArray(p.images) && p.images[0] && (p.images[0].l || p.images[0].m)) || String(p.imagesCSV || "").split(",")[0];
        if (img) { const url = "https://m.media-amazon.com/images/I/" + img; $("#pImg").src = url; $("#pImg").dataset.url = url; $("#pImg").hidden = false; }
        if (p.brand && !$("#pBrand").value) $("#pBrand").value = p.brand;
      }
      $("#pStatus").textContent = p ? "" : "Keepa had no photo for this UPC. You can still post it.";
    } catch (e) { $("#pStatus").textContent = "Couldn't get the photo from Keepa. You can still post it."; }
  }
}
async function sendPost(e) {
  e.preventDefault();
  const price = parseFloat($("#pPrice").value.replace(/[$,\s]/g, ""));
  if (!(price > 0)) { $("#pStatus").innerHTML = `<span class="err">Type the price you want in the catalog.</span>`; $("#pPrice").focus(); return; }
  $("#pGo").disabled = true; $("#pStatus").textContent = "Posting…";
  const payload = { secret: catalog.secret, brand: $("#pBrand").value.trim(), image: $("#pImg").dataset.url || "", price, moq: $("#pMoq").value.trim(), exp: $("#pExp").value.trim() || "NA", upc: $("#pUpc").value };
  let res;
  try { res = await api("/api/catalog", { url: catalog.url, payload }); } catch (err) { res = { ok: false, error: "Couldn't reach the Tidy Sheets app. Is it still running?" }; }
  const u = normUpc(payload.upc);
  if (res.ok || res.duplicate) {
    posted[u] = { day: localDay(), row: res.row || null, price };
    (await dbP).doc("catalog/posted").set({ upcs: posted }).catch(() => {});
  }
  if (res.ok) { $("#postDlg").hidden = true; show(); return; }
  $("#pGo").disabled = false;
  $("#pStatus").innerHTML = res.duplicate ? `This UPC is already in your catalog (row ${esc(res.row)}). Marked as posted.` : `<span class="err">${esc(res.error || "Posting didn't work.")}</span>`;
  if (res.duplicate) show();
}

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
// The UPC in a cell: its run of digits (11 digits get back their lost leading zero).
// Several UPCs with a count after each, like "022000279774(18)", are a mixed floor display: no single UPC.
// Several UPCs otherwise ("193052081049; New, 845218024970; Old") are alternatives: the first one is used.
function upcDigits(v) {
  const s = String(v == null ? "" : v), groups = s.match(/\d{8,14}/g);
  if (groups && groups.length > 1 && /\d{8,14}\s*\(\s*\d+\s*\)/.test(s)) return "";
  const d = groups ? groups[0] : s.replace(/\D/g, "");
  return d.length === 11 ? "0" + d : d;
}
const normUpc = v => { const d = upcDigits(v).replace(/^0+/, ""); return d.length >= 6 ? d : ""; };
// Column names compared with underscores and run-together words split, so "UPC_CD", "AV_QTY" and "UPCCodeList" are recognized.
const cname = c => String(c.name).replace(/_/g, " ").replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
// The product UPC column: "UPC", "Item UPC", also run-together names like "UPCCHKDIG"; never a case barcode ("CASUPC", "Case UPC").
function upcColumn(R) {
  const names = R.cols.map(cname), isCase = nm => /\b(cas|case|cases|cs|ctn|carton|inner|master|pallet)/i.test(nm);
  const pick = re => names.findIndex(nm => re.test(nm) && !isCase(nm));
  const j = [pick(/\b(upc|ean|gtin|barcode)\b/i), pick(/\b(upc|ean|gtin)/i)].find(x => x >= 0);
  return j ?? names.findIndex(nm => /\b(upc|ean|gtin|barcode)\b/i.test(nm));
}
function amzOf(r) {
  if (cur.upj < 0) return undefined;
  const k = normUpc(r.val[cur.upj]);
  if (!k) return amz.map.size ? 0 : undefined; // no UPC: can't be matched on Amazon
  const e = amz.map.get(k);
  if (!e) return undefined;
  return e.length === 1 ? 0 : { day: e[0], asin: e[1], price: e[2], rank: e[3], title: e[4], ref: e.length >= 7 ? e[5] : null, fba: e.length >= 7 ? e[6] : null, sold: e.length >= 8 ? e[7] : undefined, count: e.length >= 9 ? e[8] : null, drops30: e.length >= 11 ? e[9] : null, drops90: e.length >= 11 ? e[10] : null };
}
// Your cost per unit: "Low Sell" first, then other cost-like columns (never retail or comparison prices).
function costColumn(R) {
  const num = R.cols.map((c, j) => j).filter(j => ["number", "currency"].includes(R.cols[j].type));
  const find = re => num.find(j => re.test(cname(R.cols[j])) && !/compar|retail|msrp|amazon|list/i.test(cname(R.cols[j])));
  const base = find(/\blow ?sell\b/i) ?? find(/\bcost\b|wholesale|\bunit ?(price|cost)\b/i) ?? find(/\bprice\b/i) ?? -1;
  // A price that includes tariff, duty or delivery ("+ Tariff", "Landed") is what you really pay, when it's at least the base price.
  const extra = num.find(j => j !== base && /\b(tariff|tariffs|landed|duty|duties|delivered|freight incl\w*)\b/i.test(cname(R.cols[j])));
  if (base >= 0 && extra != null) {
    const both = R.rows.filter(r => typeof r.val[base] === "number" && typeof r.val[extra] === "number" && r.val[base] > 0);
    if (both.length && both.filter(r => r.val[extra] >= r.val[base] - 0.001).length >= both.length * 0.8) return extra;
  }
  return base;
}
// Profit and ROI for one product: Amazon price minus referral fee, FBA fee and your cost.
// How many items an Amazon listing holds ("Pack of 12"), from Keepa's number of items, else its title.
function titlePack(t) {
  const m = String(t || "").match(/\bpack of (\d{1,3})\b/i) || String(t || "").match(/\b(\d{1,3})[- ]?(pack|pk)\b/i);
  return m ? +m[1] : 0;
}
// How many items the supplier's own unit already holds ("3pk", "Pack of 2"), from its description.
function vendorPack(r) {
  const dj = descColumn(cur.R);
  return dj >= 0 ? titlePack(r.val[dj]) || 1 : 1;
}
// "12 Count" in an Amazon title, unless the supplier's description has the same count (a 120-count bottle is one bottle).
function titleCount(t, vendorText) {
  const m = String(t || "").match(/\b(\d{1,3})\s?(count|ct)\b/i);
  if (!m) return 0;
  return new RegExp("\\b" + m[1] + "\\s?(count|ct)\\b", "i").test(String(vendorText || "")) ? 0 : +m[1];
}
// Pack counts you set by hand per ASIN (the ×N link under Our cost): packs/overrides {asin: n}.
let packOverrides = {};
dbP.then(db => db.doc("packs/overrides").get()).then(s => { packOverrides = (s.exists && s.data().asins) || {}; if (cur) show(); }).catch(() => {});
// Units of the supplier's item needed for one Amazon listing: a 12-pack listing of a single tube = 12.
// Keepa's item count and the title can disagree (sellers enter it wrong), so the larger one wins.
function packMult(r, a) {
  if (a.asin && packOverrides[a.asin] > 0) return packOverrides[a.asin];
  const dj = descColumn(cur.R), vendorText = dj >= 0 ? r.val[dj] : "";
  const amazonCount = Math.max(typeof a.count === "number" && a.count > 0 ? a.count : 0, titlePack(a.title), titleCount(a.title, vendorText), 1);
  const mine = vendorPack(r);
  return amazonCount >= 2 * mine ? Math.round(amazonCount / mine) : 1;
}
// Offer mode: the supplier wants you to name a price, so instead of ROI on their cost the app shows the most you can pay
// per unit and still make MIN_ROI. On by itself for files with no price column, or with the Offer mode switch.
let offerToggle = false;
try { offerToggle = localStorage.getItem("tidyOfferMode") === "1"; } catch (e) { /* off */ }
const offerMode = () => !!cur && (cur.cj < 0 || offerToggle);
function econ(r) {
  const a = amzOf(r);
  if (!a || !a.price) return null;
  const mult = packMult(r, a), price = a.price / 100;
  const refPct = typeof a.ref === "number" && a.ref > 0 ? a.ref : EST_REF_PCT;
  const fba = typeof a.fba === "number" && a.fba >= 0 ? a.fba / 100 : EST_FBA / 100;
  const fees = price * refPct / 100 + fba, est = !(typeof a.ref === "number" && a.ref > 0 && typeof a.fba === "number" && a.fba >= 0);
  if (offerMode()) {
    const maxListing = (price - fees) / (1 + MIN_ROI);
    return { offer: true, price, mult, fees, est, maxListing, maxUnit: maxListing / mult, cost: maxListing, profit: price - fees - maxListing, roi: MIN_ROI };
  }
  const unit = cur.cj >= 0 && typeof r.val[cur.cj] === "number" ? r.val[cur.cj] : null, cost = unit == null ? null : unit * mult;
  const profit = cost == null ? null : price - fees - cost;
  return { price, unit, mult, cost, fees, profit, roi: cost ? profit / cost : null, est };
}
// Why an on-Amazon product doesn't make the cut, or null if it does.
function failReason(r) {
  const a = amzOf(r);
  if (!a) return null;
  if (!a.price) return "No Amazon price";
  if (a.price < MIN_AMZ_PRICE) return "Under $" + (MIN_AMZ_PRICE / 100).toFixed(2);
  if (!a.rank) return "No sales rank";
  if (a.rank > MAX_RANK) return "Rank over " + n(MAX_RANK);
  // Amazon's "bought in past month" is missing on many listings that sell well. Without it, sales are estimated from
  // Keepa's sales-rank drops; with neither, sales don't reject the product (the rank rule decides).
  if (a.sold !== undefined && !(a.sold >= MIN_SOLD)) {
    const est = salesEstimate(a);
    if (est != null && est < MIN_SOLD) return `Under ${MIN_SOLD} sales/month`;
  }
  const e = econ(r);
  if (e && e.offer) return e.maxUnit > 0 ? null : "No room for profit";
  if (e && e.roi != null && e.roi < MIN_ROI) return "ROI " + Math.round(e.roi * 100) + "%";
  return null;
}
// Monthly sales estimated from how often the sales rank dropped (each drop is roughly one sale), or null when Keepa has no count.
function salesEstimate(a) {
  const v = [a.drops30, typeof a.drops90 === "number" && a.drops90 >= 0 ? a.drops90 / 3 : null].filter(x => typeof x === "number" && x >= 0);
  return v.length ? Math.round(Math.max(...v)) : null;
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
function rankDrops(p, days) {
  const v = p.stats && p.stats["salesRankDrops" + days];
  return typeof v === "number" && v >= 0 ? v : -1;
}
function listingCount(p) {
  return Math.max(...[p.numberOfItems, p.packageQuantity].filter(v => typeof v === "number" && v > 0), titlePack(p.title), 1);
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
  $("#scanBtn").hidden = !!scanning; $("#stopBtn").hidden = !scanning; $("#refreshBtn").hidden = true;
  if (scanning) return;
  if (cur.upj < 0) { $("#scanBtn").disabled = true; amzStatus("No UPC column in this file, so it can't be checked on Amazon."); return; }
  $("#scanBtn").disabled = false;
  const rows = cur.kept.filter(r => !bannedBy(r) && !isRejected(r)), states = rows.map(amzOf);
  const checked = states.filter(s => s !== undefined).length, on = states.filter(s => s && s.asin).length;
  const pass = rows.filter(r => { const s = amzOf(r); return s && s.asin && !failReason(r); }).length;
  $("#refreshBtn").hidden = !pass; $("#refreshBtn").textContent = `Refresh prices (${n(pass)} in Kept)`;
  const noFees = states.filter(s => s && s.asin && s.sold === undefined).length;
  amzStatus(checked
    ? `${n(checked)} of ${n(rows.length)} checked · ${n(on)} on Amazon · <b class="ok">${n(pass)} pass: $${(MIN_AMZ_PRICE / 100).toFixed(2)}+, ${offerMode() ? "" : Math.round(MIN_ROI * 100) + "%+ ROI, "}rank under ${n(MAX_RANK)}, ${MIN_SOLD}+ sold in a month</b>`
      + (noFees ? ` · press Scan to get fees and monthly sales for ${n(noFees)} products checked before` : "")
      + (offerMode() ? ` · <b>Offer mode:</b> Max offer = the most to pay per unit for ${Math.round(MIN_ROI * 100)}% ROI` + (cur.cj < 0 ? " (this file has no prices)" : "") : "")
    : `Checks each kept product's UPC on Keepa and keeps only the ones on Amazon at ${Math.round(MIN_ROI * 100)}%+ ROI.`);
  $("#keyChange").hidden = !keepaKey;
  $("#offerSw").checked = offerMode(); $("#offerSw").disabled = cur.cj < 0;
  $("#offerSw").parentElement.title = cur.cj < 0 ? "This file has no prices, so Offer mode is always on" : "Ignore the file's prices and show the most you can pay per unit for 40% ROI";
}
async function scan(refreshKept) {
  if (scanning || !cur || cur.upj < 0) return;
  refreshKept = refreshKept === true;
  if (!(await loadKey())) { $("#keyForm").hidden = false; $("#keyIn").focus(); amzStatus("Paste your Keepa API key once. It's saved on this Mac."); return; }
  const todo = [], seenK = new Set();
  for (const r of cur.kept) {
    if (bannedBy(r) || isRejected(r)) continue;
    const raw = upcDigits(r.val[cur.upj]), k = normUpc(raw);
    if (!k || seenK.has(k)) continue;
    seenK.add(k);
    const e = amz.map.get(k);
    if (refreshKept ? (e && e.length > 1 && !failReason(r)) : (!e || (e.length > 1 && e.length < 8))) todo.push({ k, raw }); // results never expire; only products never checked (or saved before fees and monthly sales were stored) are fetched
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
        // The price you'd realistically sell at, erring low so ROI is never overstated: the lowest of the buy box,
        // Amazon's own offer and the cheapest FBA offer. The cheapest new offer of any kind is only a last resort (it may add shipping).
        const pos = v => typeof v === "number" && v > 0;
        const main = [st.buyBoxPrice, curv[0], curv[10]].filter(pos);
        const price = main.length ? Math.min(...main) : (pos(curv[1]) ? curv[1] : 0);
        const ref = [p.referralFeePercentage, p.referralFeePercent].find(v => typeof v === "number" && v > 0) ?? null;
        const fba = p.fbaFees && typeof p.fbaFees.pickAndPackFee === "number" && p.fbaFees.pickAndPackFee >= 0 ? p.fbaFees.pickAndPackFee : null;
        const entry = [today, p.asin, price, typeof curv[3] === "number" && curv[3] > 0 ? curv[3] : 0, String(p.title || "").slice(0, 200), ref, fba, bestMonth(p), listingCount(p), rankDrops(p, 30), rankDrops(p, 90)];
        for (const c of codes) if (!hit.has(c)) hit.set(c, entry);
      }
      if (batch.length === 1 && j.products.length && !hit.size) { const p = j.products[0]; hit.set(batch[0].k, [today, p.asin, 0, 0, String(p.title || "").slice(0, 200), null, null, bestMonth(p), listingCount(p), rankDrops(p, 30), rankDrops(p, 90)]); }
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
  const nm = j => cname(R.cols[j]);
  const notQty = /\b(so|sold|sales|order|ordered|orders|on order|po|min|moq|case|cs|inner|pack|pk|pallet|reserved|alloc|allocated|committed|incoming|backorder)\b/i;
  const find = re => num.find(j => re.test(nm(j)) && !notQty.test(nm(j)));
  // "ava?i?l…" also catches misspellings like "Avalible".
  return find(/\b(av|ava?i?l\w*)\b.*\b(qty|quantity)\b|\b(qty|quantity)\b.*\b(av|ava?i?l\w*)\b/i)  // "AV QTY", "Qty. Available"
    ?? find(/\b(ava?i?l\w*|in ?stock|on ?hand)\b/i)                                                       // "IN STOCK", "On Hand", "Cases Avalible"
    ?? find(/\b(qty|quantity)\b/i)
    ?? find(/\b(stock|units|inventory)\b/i) ?? -1;
}
// When the quantity is counted in cases ("Cases Available"), the pack-size column turns it into units.
function packColumn(R, qj) {
  if (!/\b(cases?|cs|ctns?|cartons?|boxes)\b/i.test(cname(R.cols[qj]))) return -1;
  const num = R.cols.map((c, j) => j).filter(j => j !== qj && R.cols[j].type === "number");
  const find = re => num.find(j => re.test(cname(R.cols[j])));
  return find(/\b(pack ?size|case ?pack|cs ?pk|units? per (case|cs|ctn)|per (case|cs|ctn))\b/i) ?? find(/\b(pack|pk|count|ct)\b/i) ?? -1;
}
// The column that names each product: an ID-like column whose values are (nearly) all filled and different.
function idColumn(R) {
  const ok = j => { const v = R.rows.map(r => r.val[j]).filter(x => x != null); return v.length >= R.rows.length * 0.95 && new Set(v.map(String)).size >= v.length * 0.95; };
  const js = R.cols.map((c, j) => j);
  return js.find(j => /\b(id|sku|item|product ?(no|number|#)|part)\b|#/i.test(cname(R.cols[j])) && ok(j))
    ?? js.find(j => /\b(upc|ean|gtin|barcode|code)\b/i.test(cname(R.cols[j])) && ok(j))
    ?? js.find(j => R.cols[j].type === "text" && ok(j)) ?? -1;
}
// What a product is remembered by (seen, rejected, posted): its ID, or its UPC when the ID cell is empty or there is no ID column.
const keyOf = r => {
  const id = cur.idj >= 0 && r.val[cur.idj] != null ? String(r.val[cur.idj] instanceof Date ? isoD(r.val[cur.idj]) : r.val[cur.idj]).trim().toUpperCase() : "";
  if (id) return id;
  const u = cur.upj >= 0 ? normUpc(r.val[cur.upj]) : "";
  return u ? "UPC:" + u : null;
};
const localDay = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
const niceDate = iso => { try { return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); } catch (e) { return iso; } };

// Some vendors list the same UPC more than once (different lots or IDs). Keep one row per UPC: a listing with
// enough stock first, then the lowest cost, then the most stock. The others are kept on the row for the ×2 tag.
function mergeSameUpc(R, upj, units, cj) {
  if (upj < 0) return 0;
  const groups = new Map(), order = [];
  for (const r of R.rows) {
    const k = normUpc(r.val[upj]);
    if (!k) { order.push(r); continue; }
    if (groups.has(k)) groups.get(k).push(r); else { groups.set(k, [r]); order.push(k); }
  }
  const qty = r => units(r) ?? -1;
  const cost = r => cj >= 0 && typeof r.val[cj] === "number" ? r.val[cj] : Infinity;
  let merged = 0;
  R.rows = order.map(x => {
    if (typeof x !== "string") return x;
    const g = groups.get(x);
    if (g.length === 1) return g[0];
    // Same price everywhere: the same offer from several warehouses, so the quantities add up.
    if (g.every(r => Math.abs(cost(r) - cost(g[0])) < 0.005 || cost(r) === cost(g[0]))) {
      const best = g[0], parts = g.map(r => ({ r, qty: qty(r) }));
      const qtyCols = R.cols.map((c, j) => j).filter(j => R.cols[j].type === "number" && /\b(ava?i?l\w*|in ?stock|on ?hand|units|cases|qty|quantity)\b/i.test(cname(R.cols[j])) && !/\b(case ?pack|pack|per|pallet|pl)\b/i.test(cname(R.cols[j])));
      const sums = qtyCols.map(j => g.reduce((t, r) => t + (typeof r.val[j] === "number" ? r.val[j] : 0), 0));
      best.parts = parts.map(p => ({ val: p.r.val.slice(), qty: p.qty }));
      qtyCols.forEach((j, i) => { best.val[j] = sums[i]; });
      best.summed = true;
      best.others = g.slice(1);
      merged += best.others.length;
      return best;
    }
    const best = g.slice().sort((a, b) => (qty(b) >= MIN_QTY) - (qty(a) >= MIN_QTY) || cost(a) - cost(b) || qty(b) - qty(a))[0];
    best.others = g.filter(r => r !== best);
    merged += best.others.length;
    return best;
  });
  return merged;
}
// Some suppliers list a product once with its prices ("Kind Words Matte Lip Liner  $16.00  $6.00") and its shades
// underneath with only a UPC and quantity. Each shade gets the product's prices and name; the product line and
// category rows ("LIPS") are not products themselves.
function fillFamilies(R, upj, qj, cj) {
  if (upj < 0 || cj < 0) return 0;
  const text = R.cols.map((c, j) => j).filter(j => j !== upj && R.cols[j].type === "text");
  const dj = text.find(j => /\b(desc\w*|title|product|item name|name)\b/i.test(cname(R.cols[j]))) ?? text[0] ?? -1;
  const nums = R.cols.map((c, j) => j).filter(j => j !== upj && ["number", "currency", "percent"].includes(R.cols[j].type));
  let fam = null, filled = 0;
  const drop = new Set();
  for (const r of R.rows) {
    if (!normUpc(r.val[upj])) {
      const has = nums.filter(j => typeof r.val[j] === "number");
      const label = dj >= 0 && r.val[dj] != null ? String(r.val[dj]) : "";
      if (label && has.includes(cj) && (qj < 0 || r.val[qj] == null)) fam = { r, label, has }; // a product line: prices but no quantity
      else if (!has.length && r.val.filter(v => v != null && v !== "").length <= 1) { fam = null; drop.add(r); } // a category row like "LIPS"
      continue;
    }
    if (!fam) continue;
    if (r.val[cj] != null) { fam = null; continue; }                   // a product with its own price: the line has ended
    const missing = fam.has.filter(j => r.val[j] == null);
    missing.forEach(j => { r.val[j] = fam.r.val[j]; });
    if (dj >= 0) {
      const d = r.val[dj] == null ? "" : String(r.val[dj]);
      if (!d.toLowerCase().includes(fam.label.toLowerCase().slice(0, 14))) r.val[dj] = fam.label + (d ? " – " + d : "");
    }
    fam.used = true; drop.add(fam.r); filled++;
  }
  if (drop.size) R.rows = R.rows.filter(r => !drop.has(r));
  return filled;
}
function compute() {
  const R = organize(sheets[active].aoa, OPTS);
  const upj = upcColumn(R), qj = qtyColumn(R), cj = costColumn(R), pj = qj >= 0 ? packColumn(R, qj) : -1;
  const families = fillFamilies(R, upj, qj, cj);
  // Units in stock: the quantity, times the pack size when the quantity is in cases.
  const units = r => qj < 0 || typeof r.val[qj] !== "number" ? null
    : pj >= 0 && typeof r.val[pj] === "number" && r.val[pj] > 0 ? r.val[qj] * r.val[pj] : r.val[qj];
  const merged = mergeSameUpc(R, upj, units, cj);
  const all = sortRows(R);
  const low = r => !(units(r) >= MIN_QTY);
  cur = { R, qj, pj, units, merged, families, kept: qj < 0 ? all : all.filter(r => !low(r)), rejected: qj < 0 ? [] : all.filter(low) };
  cur.idj = idColumn(R);
  cur.upj = upj;
  cur.cj = cj;
  // The supplier's expiration date ("EXP", "Best By", "BIUB"), shown in place of the sales rank.
  cur.ej = R.cols.findIndex(c => /\b(exp|exp date|expiry|expiration|expires|best ?by|best before|bbd|biub|use by|sell by)\b/i.test(cname(c)));
}

/* ---------- remembering products ---------- */
async function remember() {
  const db = await dbP;
  if (!db) { hist = { status: "off" }; return; }
  if (!vendor) { hist = { status: "pick" }; return; }
  if (cur.idj < 0 && cur.upj < 0) { hist = { status: "noid" }; return; }
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
    const next = { ...(meta || {}), name: vendor.name, prefixes, idColumn: cur.idj >= 0 ? cur.R.cols[cur.idj].name : "UPC", uploads: ((meta && meta.uploads) || 0) + 1, lastUpload: new Date().toISOString(), lastFile: fileName, products: seen.size + fresh.length };
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
  const rank = e => !e ? -1e9 : e.offer ? e.profit : (e.roi ?? -1e9); // offer mode: biggest profit first
  const byRoi = rows => rows.slice().sort((x, y) => rank(econ(y)) - rank(econ(x)));
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
    + `<span class="fname">${esc(fileName)}${cur.merged ? ` · ${n(cur.merged)} repeat listings combined` : ""}${cur.families ? ` · ${n(cur.families)} shades given their product's price` : ""}${qj < 0 ? " · no quantity column found, nothing rejected" : (cur.pj >= 0 ? ` · under ${MIN_QTY} units (“${esc(R.cols[qj].name)}” × “${esc(R.cols[cur.pj].name)}”) is rejected` : ` · under ${MIN_QTY} in “${esc(R.cols[qj].name)}” is rejected`)}</span></div>`;

  $("#tabs").hidden = sheets.length < 2;
  $("#tabs").innerHTML = sheets.map((s, i) => `<button class="tab" role="tab" type="button" data-i="${i}" aria-selected="${i === active}">${esc(s.name)}</button>`).join("");
  if (!R.cols.length) { $("#sheet").innerHTML = `<div class="more">This sheet is empty.</div>`; return; }
  const cls = t => ["number", "currency", "percent"].includes(t) ? "n" : t === "date" ? "d" : "";
  const badge = view === "kept" && freshRows.length, reason = view === "banned" || view === "low";
  const act = (cur.idj >= 0 || cur.upj >= 0) && ["kept", "new", "low", "noamz", "mine"].includes(view);
  const postBtn = r => {
    if (!(view === "kept" || view === "new") || cur.upj < 0) return "";
    const u = normUpc(r.val[cur.upj]);
    if (!u) return "";
    const done = posted[u];
    return done ? `<span class="pill posted" title="Posted ${esc(done.day || "")}${done.row ? ", row " + done.row : ""}">Posted ✓</span>`
      : `<button class="rowbtn post" type="button" data-post="${esc(keyOf(r))}">Post</button>`;
  };
  const actTd = r => { const k = keyOf(r); return !k ? "<td></td>" : view === "mine"
    ? `<td><button class="rowbtn" type="button" data-undo="${esc(k)}">Undo</button></td>`
    : `<td class="nw"><button class="rowbtn rej" type="button" data-rej="${esc(k)}" title="Hide this product from ${esc(vendor ? vendor.name : "this company")} for good">Reject</button>${postBtn(r)}</td>`; };
  const reasonOf = r => view === "banned" ? why.get(r) : failWhy.get(r);
  const amzCols = (view === "kept" || view === "new" || view === "low") && rows.some(r => amzOf(r));
  const money = c => c ? "$" + (c / 100).toFixed(2) : "";
  const usd = v => v == null ? "" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
  const amzTd = r => {
    const a = amzOf(r);
    if (!a) return `<td class="more">${a === undefined ? "not checked" : ""}</td><td></td><td></td><td></td><td></td><td></td><td></td>`;
    const e = econ(r), star = e && e.est ? `<span title="Estimated fees: ${EST_REF_PCT}% + $${(EST_FBA / 100).toFixed(2)}">*</span>` : "";
    return `<td class="nw"><a href="https://www.amazon.com/dp/${esc(a.asin)}" target="_blank" rel="noopener" title="${esc(a.title)}${a.rank ? " · Sales rank " + n(a.rank) : ""}">${esc(a.asin)}</a></td><td class="n" title="Lowest of buy box, Amazon and FBA offers, checked ${esc(a.day || "")}">${money(a.price)}</td>`
      + (e && e.offer
        ? `<td class="n" title="Pay at most this per unit to make ${Math.round(MIN_ROI * 100)}% ROI (${usd(e.maxListing)} for the whole Amazon listing)"><b>${usd(e.maxUnit)}</b><span class="sub">per unit</span><button class="sub packbtn" type="button" data-pack="${esc(a.asin)}" data-mult="${e.mult}" title="Units of this item in one Amazon listing. Click to correct it.">${e.mult > 1 ? `listing = ${e.mult} units` : "×1"}${packOverrides[a.asin] ? " ✎" : ""}</button></td>`
        : `<td class="n">${e && e.cost != null ? usd(e.cost) + `<button class="sub packbtn" type="button" data-pack="${esc(a.asin)}" data-mult="${e.mult}" title="Units of this item in one Amazon listing. Click to correct it.">${e.mult > 1 ? `${e.mult} × ${usd(e.unit)}` : "×1"}${packOverrides[a.asin] ? " ✎" : ""}</button>` : cur.cj >= 0 && typeof r.val[cur.cj] === "number" ? usd(r.val[cur.cj]) : ""}</td>`)
      + `<td class="n"${e ? ` title="Amazon fees ${usd(e.fees)}${e.est ? " (estimated)" : ""}"` : ""}>${e ? usd(e.profit) + star : ""}</td>`
      + `<td class="n"><b class="${e && e.roi != null && e.roi >= MIN_ROI ? "ok" : "rej"}">${e && e.roi != null ? Math.round(e.roi * 100) + "%" : ""}</b>${e && !e.offer && e.roi >= 3 ? `<span class="sub warn" title="ROI this high is usually a pack-size mix-up. Check the Amazon listing and fix the count under Our cost.">check pack</span>` : ""}</td><td class="d" title="${a.rank ? "Sales rank " + n(a.rank) : "No sales rank"}">${cur.ej >= 0 && r.val[cur.ej] != null ? esc(fmt(r.val[cur.ej], R.cols[cur.ej])) : ""}</td>${(() => {
        if (a.sold === undefined) return `<td class="n"></td>`;
        if (a.sold) return `<td class="n">${n(a.sold)}+</td>`;
        const est = salesEstimate(a);
        return est != null ? `<td class="n" title="Amazon shows no 'bought in past month'; estimated from Keepa sales-rank drops">~${n(est)}/mo <span class="sub">est.</span></td>`
          : `<td class="n" title="Amazon shows no 'bought in past month' and Keepa has no rank-drop count; the sales rank decides">no data</td>`;
      })()}`;
  };
  const dupTag = r => {
    if (r.summed) {
      // The column that tells the copies apart, e.g. the warehouse.
      const dj = R.cols.findIndex((c, j) => c.type === "text" && new Set(r.parts.map(p => p.val[j])).size > 1);
      const where = p => dj >= 0 && p.val[dj] != null ? String(p.val[dj]) : "";
      const lines = r.parts.map(p => [where(p), p.qty >= 0 ? n(p.qty) + " units" : ""].filter(Boolean).join(": "));
      return ` <span class="pill dup" title="Listed ${r.parts.length} times at the same price, so the quantities are added up: ${esc(lines.join("; "))}">×${r.parts.length} added</span>`;
    }
    const line = x => [keyOf(x), cur.units(x) != null ? n(cur.units(x)) + " units" : "", cur.cj >= 0 && typeof x.val[cur.cj] === "number" ? "$" + x.val[cur.cj].toFixed(2) : ""].filter(Boolean).join(" · ");
    return ` <span class="pill dup" title="Listed ${r.others.length + 1} times in this file. Showing ${esc(line(r))}. Also: ${esc(r.others.map(line).join("; "))}">×${r.others.length + 1}</span>`;
  };
  const pics = (sheets[active] && sheets[active].images) || {}, hasPics = Object.keys(pics).length > 0;
  // A photo/image column with no real pictures in it ("No photo", blanks) is hidden; real pictures show in the Photo column.
  const emptyPicCol = j => /\b(image|images|photo|photos|picture|pic|img)\b/i.test(cname(R.cols[j])) && R.rows.filter(r => typeof r.val[j] === "string" && /^https?:\/\//i.test(r.val[j])).length <= R.rows.length * 0.1;
  const photoTd = r => `<td class="ph">${pics[r.src] ? `<img src="${pics[r.src]}" alt="">` : ""}</td>`;
  const cellHtml = (v, j) => {
    if (typeof v === "string" && /^https?:\/\/\S+$/i.test(v)) {
      let host = "link";
      try { host = new URL(v).hostname.replace(/^www\./, ""); } catch (e) { /* keep "link" */ }
      return `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(host)}</a>`;
    }
    return esc(fmt(v, R.cols[j]));
  };
  // Total qty (units the 300 rule uses) always sits right after the UPC; the quantity column itself is hidden unless it counts cases.
  const QTY = -1, showQty = cur.qj >= 0;
  const shownCols = R.cols.map((c, j) => j).filter(j => j !== cur.upj && !(j === cur.idj && cur.upj >= 0) && !emptyPicCol(j) && !(showQty && j === cur.qj && cur.pj < 0) && !(amzCols && j === cur.ej));
  if (showQty) shownCols.unshift(QTY);
  if (cur.upj >= 0) shownCols.unshift(cur.upj);
  const keepScroll = $("#sheet").scrollTop;
  $("#sheet").innerHTML = `<table><thead><tr>${act ? "<th></th>" : ""}${hasPics ? "<th>Photo</th>" : ""}${badge ? "<th></th>" : ""}${reason ? `<th>${view === "banned" ? "Banned for" : "Why"}</th>` : ""}${amzCols ? "<th>ASIN</th><th>Amazon price</th><th>" + (offerMode() ? "Max offer" : "Our cost") + "</th><th>Profit</th><th>ROI</th><th>Best by</th><th title='Most bought in one month, past 12 months'>Best month</th>" : ""}${shownCols.map(j => j === QTY ? "<th>Total qty</th>" : `<th>${esc(R.cols[j].name)}</th>`).join("")}</tr></thead><tbody>${shown.map(r =>
    `<tr>${act ? actTd(r) : ""}${hasPics ? photoTd(r) : ""}${badge ? `<td>${isNew(r) ? '<span class="pill">NEW</span>' : ""}</td>` : ""}${reason ? `<td><span class="pill bad">${esc(reasonOf(r))}</span></td>` : ""}${amzCols ? amzTd(r) : ""}${shownCols.map(j => j === QTY ? [cur.units(r), QTY] : [r.val[j], j]).map(([v, j]) => j === QTY ? `<td class="n"><b>${v == null ? "" : n(v)}</b>${r.qtyNotes && r.qtyNotes[cur.qj] ? `<span class="sub">${esc(r.qtyNotes[cur.qj])}</span>` : ""}</td>` : `<td class="${typeof v === "string" ? (/\s/.test(v) ? (v.length > 24 ? "w" : "") : (v.length <= 20 ? "nw" : "")) : cls(R.cols[j].type)}">${cellHtml(v, j)}${j === cur.upj && r.others ? dupTag(r) : ""}</td>`).join("")}</tr>`).join("")}</tbody>${view === "kept" && R.totalRows.length ? `<tfoot>${R.totalRows.map(r => `<tr>${act ? "<td></td>" : ""}${hasPics ? "<td></td>" : ""}${badge ? "<td></td>" : ""}${amzCols ? "<td></td><td></td><td></td><td></td><td></td><td></td><td></td>" : ""}${shownCols.map(j => j === QTY ? "<td></td>" : `<td>${esc(r.val[j] ?? "")}</td>`).join("")}</tr>`).join("")}</tfoot>` : ""}</table>${rows.length > LIMIT ? `<div class="more">Showing the first ${n(LIMIT)} of ${n(rows.length)} rows.</div>` : ""}`;
  $("#sheet").scrollTop = keepScroll;
}

// Product pictures that float over a sheet (Excel drawings), keyed by the sheet row they sit on (1 = first row read).
function sheetPictures(buf, wb) {
  const out = {}, REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
  let zip;
  try { zip = XLSX.CFB.read(new Uint8Array(buf), { type: "array" }); } catch (e) { return out; }
  const at = new Map(zip.FullPaths.map((p, i) => [p.replace(/^Root Entry\//, "").toLowerCase(), i]));
  const bytes = p => { const i = at.get(p.toLowerCase()), c = i == null ? null : zip.FileIndex[i].content; return c ? (c instanceof Uint8Array ? c : new Uint8Array(c)) : null; };
  const xml = p => { const b = bytes(p); return b ? new DOMParser().parseFromString(new TextDecoder().decode(b), "application/xml") : null; };
  const rels = p => {
    const d = xml(p.replace(/([^/]+)$/, "_rels/$1.rels")), m = {};
    if (d) for (const r of d.getElementsByTagNameNS("*", "Relationship")) m[r.getAttribute("Id")] = { target: r.getAttribute("Target") || "", type: r.getAttribute("Type") || "" };
    return m;
  };
  const resolve = (base, t) => {
    if (t.startsWith("/")) return t.slice(1);
    const parts = base.split("/"); parts.pop();
    for (const seg of t.split("/")) { if (seg === "..") parts.pop(); else if (seg && seg !== ".") parts.push(seg); }
    return parts.join("/");
  };
  const b64 = u8 => { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); };
  const TYPES = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", gif: "image/gif", webp: "image/webp" };
  try {
    const book = xml("xl/workbook.xml"), bookRels = rels("xl/workbook.xml");
    if (!book) return out;
    for (const sh of book.getElementsByTagNameNS("*", "sheet")) {
      const name = sh.getAttribute("name"), rel = bookRels[sh.getAttributeNS(REL, "id") || sh.getAttribute("r:id")];
      if (!rel || !wb.Sheets[name]) continue;
      const sheetPath = resolve("xl/workbook.xml", rel.target), ref = wb.Sheets[name]["!ref"];
      const start = ref ? XLSX.utils.decode_range(ref).s.r : 0, found = {};
      for (const d of Object.values(rels(sheetPath)).filter(x => /\/drawing$/.test(x.type))) {
        const dPath = resolve(sheetPath, d.target), dRels = rels(dPath), doc = xml(dPath);
        if (!doc) continue;
        for (const kind of ["oneCellAnchor", "twoCellAnchor"]) for (const a of doc.getElementsByTagNameNS("*", kind)) {
          const from = a.getElementsByTagNameNS("*", "from")[0], blip = a.getElementsByTagNameNS("*", "blip")[0];
          const rowEl = from && from.getElementsByTagNameNS("*", "row")[0];
          if (!rowEl || !blip) continue;
          const media = dRels[blip.getAttributeNS(REL, "embed") || blip.getAttribute("r:embed")];
          if (!media) continue;
          const mPath = resolve(dPath, media.target), type = TYPES[mPath.split(".").pop().toLowerCase()], b = type && bytes(mPath);
          const row = +rowEl.textContent - start + 1;
          if (b && !found[row]) found[row] = "data:" + type + ";base64," + b64(b);
        }
      }
      if (Object.keys(found).length) out[name] = found;
    }
  } catch (e) { /* show the sheet without pictures */ }
  return out;
}
async function loadFile(f) {
  if (!f) return;
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  let buf = null;
  try {
    const wb = ["csv", "tsv", "txt"].includes(ext)
      ? XLSX.read(await f.text(), { type: "string", raw: true })
      : XLSX.read(buf = await f.arrayBuffer(), { type: "array" });
    const pics = buf ? sheetPictures(buf, wb) : {};
    // Long whole numbers (UPCs stored as numbers) are read in full: Excel may display them as 8.15766E+11, which drops digits.
    for (const name of wb.SheetNames) {
      const ws = wb.Sheets[name];
      for (const addr in ws) {
        const c = ws[addr];
        if (addr[0] !== "!" && c && c.t === "n" && Number.isInteger(c.v) && Math.abs(c.v) >= 1e9) c.w = String(c.v);
      }
    }
    const list = wb.SheetNames.map(s => ({ name: s, images: pics[s] || null, aoa: XLSX.utils.sheet_to_json(wb.Sheets[s], { header: 1, raw: false, defval: null, blankrows: true, dateNF: "yyyy-mm-dd" }) })).filter(s => s.aoa.length);
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
  return t.find(j => /\b(desc\w*|title|product|item name|name)\b/i.test(cname(R.cols[j]))) ?? t.find(j => j !== cur.idj && j !== cur.upj) ?? -1;
}
function recordOffers() {
  if (!vendor || !cur || cur.upj < 0) return;
  const R = cur.R, dj = descColumn(R), num = v => typeof v === "number" ? v : null, rows = [];
  for (const r of R.rows) {
    const raw = upcDigits(r.val[cur.upj]), upc = normUpc(raw);
    if (!upc) continue;
    rows.push({ upc, upc_raw: raw.length === 11 ? "0" + raw : raw, cost: cur.cj >= 0 ? num(r.val[cur.cj]) : null, qty: cur.units(r),
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

/* ---------- new files in Downloads ---------- */
// Spreadsheets saved to ~/Downloads after the last one opened or dismissed are offered in a bar with an Open button.
let dlHandled = 0, dlFiles = [];
try { dlHandled = +localStorage.getItem("tidyDlHandled") || 0; } catch (e) { /* offered again next time */ }
if (!dlHandled) { dlHandled = Date.now() / 1000; try { localStorage.setItem("tidyDlHandled", String(dlHandled)); } catch (e) { /* first visit */ } }
function markHandled(mtime) {
  dlHandled = Math.max(dlHandled, mtime);
  try { localStorage.setItem("tidyDlHandled", String(dlHandled)); } catch (e) { /* this visit only */ }
}
function renderDlBar() {
  const fresh = dlFiles.filter(f => f.mtime > dlHandled + 0.5), bar = $("#dlBar");
  bar.hidden = !fresh.length;
  if (!fresh.length) return;
  const f = fresh[0];
  bar.innerHTML = `<b>New file in Downloads:</b> <span class="fn">${esc(f.name)}</span><span class="grow"></span>`
    + `<button class="btn sm" type="button" data-open="${esc(f.name)}">Open</button>`
    + (fresh.length > 1 ? `<button class="btn sm ghost" type="button" data-more="1">${fresh.length - 1} more</button>` : "")
    + `<button class="btn sm ghost" type="button" data-dismiss="1">${fresh.length > 1 ? "Dismiss all" : "Dismiss"}</button>`;
}
async function checkDownloads() {
  if (document.visibilityState !== "visible") return;
  try { dlFiles = (await api("/api/downloads")).files || []; } catch (e) { return; }
  renderDlBar();
}
async function openDownload(name) {
  const f = dlFiles.find(x => x.name === name);
  try {
    const r = await fetch("/api/download?name=" + encodeURIComponent(name), { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    const blob = await r.blob();
    if (f) markHandled(f.mtime);
    renderDlBar();
    await loadFile(new File([blob], name));
  } catch (e) {
    $("#dlBar").innerHTML = `<span class="err">Couldn't open “${esc(name)}” from Downloads.</span> Drag it onto this page instead.`;
  }
}
$("#dlBar").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  const fresh = dlFiles.filter(f => f.mtime > dlHandled + 0.5);
  if (b.dataset.open) openDownload(b.dataset.open);
  else if (b.dataset.dismiss) { markHandled(fresh[0].mtime); renderDlBar(); }
  else if (b.dataset.more) {
    $("#dlBar").innerHTML = `<b>New files in Downloads:</b>` + fresh.map(f => `<button class="btn sm ghost" type="button" data-open="${esc(f.name)}">${esc(f.name)}</button>`).join("")
      + `<button class="btn sm ghost" type="button" data-dismiss-all="1">Dismiss all</button>`;
  } else if (b.dataset.dismissAll) { markHandled(Math.max(...fresh.map(f => f.mtime))); renderDlBar(); }
});
setInterval(checkDownloads, 4000);
document.addEventListener("visibilitychange", checkDownloads);
window.addEventListener("focus", checkDownloads);
checkDownloads();

/* ---------- events ---------- */
$("#file").addEventListener("change", e => {
  const f = e.target.files[0];
  const d = f && dlFiles.find(x => x.name === f.name);
  if (d) { markHandled(d.mtime); renderDlBar(); }
  loadFile(f); e.target.value = "";
});
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
  const kb = e.target.closest("button[data-pack]");
  if (kb) {
    const asin = kb.dataset.pack, now = +kb.dataset.mult || 1;
    const v = window.prompt(`How many of this item are in Amazon listing ${asin}?\n(1 = a single item, 12 = a 12-pack)`, String(now));
    if (v === null) return;
    const nnum = Math.round(+String(v).replace(/[^\d.]/g, ""));
    if (!(nnum >= 1 && nnum <= 500)) return;
    packOverrides[asin] = nnum;
    dbP.then(db => db.doc("packs/overrides").set({ asins: packOverrides })).catch(() => {});
    return show();
  }
  const pb = e.target.closest("button[data-post]");
  if (pb) return openPost(pb.dataset.post);
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
$("#scanBtn").addEventListener("click", () => scan(false));
$("#offerSw").addEventListener("change", e => {
  offerToggle = e.target.checked;
  try { localStorage.setItem("tidyOfferMode", offerToggle ? "1" : "0"); } catch (err) { /* this visit only */ }
  show();
});
$("#refreshBtn").addEventListener("click", () => scan(true));
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
$("#postForm").addEventListener("submit", sendPost);
$("#pCancel").addEventListener("click", () => { $("#postDlg").hidden = true; postRow = null; });
$("#postDlg").addEventListener("click", e => { if (e.target.id === "postDlg") { $("#postDlg").hidden = true; postRow = null; } });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#postDlg").hidden) { $("#postDlg").hidden = true; postRow = null; } });
$("#pSetup").addEventListener("click", () => { $("#postDlg").hidden = true; openSetup(); });
$("#catCopy").addEventListener("click", async () => {
  await catalogReady;
  const text = catalogScript();
  try { await navigator.clipboard.writeText(text); $("#catCopyNote").textContent = "Copied. Paste it into Apps Script."; $("#catScript").hidden = true; }
  catch (err) { $("#catScript").hidden = false; $("#catScript").value = text; $("#catScript").select(); $("#catCopyNote").textContent = "Press Cmd+C to copy the selected script."; }
});
$("#catForm").addEventListener("submit", async e => {
  e.preventDefault();
  const url = $("#catUrl").value.trim();
  if (!/^https:\/\/script\.google\.com\/macros\/s\/[\w-]+\/exec$/.test(url)) { $("#catNote").innerHTML = `<span class="err">That doesn't look right. Copy the Web app URL that ends in /exec.</span>`; return; }
  await catalogReady;
  catalog.url = url;
  await (await dbP).doc("settings/catalog").set(catalog);
  $("#catNote").textContent = "Saved. Click Post on any product to send it to your catalog.";
  setTimeout(() => { $("#catSetup").hidden = true; }, 1500);
});
$("#catClose").addEventListener("click", () => { $("#catSetup").hidden = true; });
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

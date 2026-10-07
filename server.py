#!/usr/bin/env python3
"""
Kr_Url_Extractor — hosted Flask backend.
"""
import asyncio
import base64
import binascii
import json
import os
import re

import aiohttp
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".")
CORS(app)

FETCH_TIMEOUT = 8
MAX_HTML = 4 * 1024 * 1024
MAX_ASSETS = 10
CONCURRENCY = 16

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

FIREBASE_PATTERNS = [
    re.compile(r'https?://[a-z0-9-]+(?:-default-rtdb)?(?:\.[a-z0-9-]+)*\.firebaseio\.com', re.I),
    re.compile(r'https?://[a-z0-9-]+(?:-default-rtdb)?(?:\.[a-z0-9-]+)*\.firebasedatabase\.app', re.I),
    re.compile(r'https?://[a-z0-9-]+\.firebaseapp\.com', re.I),
    re.compile(r'https?://[a-z0-9-]+\.web\.app', re.I),
    re.compile(r'https?://[a-z0-9-]+\.cloudfunctions\.net', re.I),
    re.compile(r'https?://firebasestorage\.googleapis\.com/v0/b/[a-z0-9-]+\.appspot\.com', re.I),
]

CONFIG_KEY_PATTERN = re.compile(
    r'["\']?(apiKey|authDomain|databaseURL|projectId|storageBucket|'
    r'messagingSenderId|appId|measurementId)["\']?\s*[:=]\s*["\']([^"\']{1,300})["\']',
    re.I,
)

KEYMAP = {
    "apikey": "apiKey", "authdomain": "authDomain", "databaseurl": "databaseURL",
    "projectid": "projectId", "storagebucket": "storageBucket",
    "messagingsenderid": "messagingSenderId", "appid": "appId",
    "measurementid": "measurementId",
}

SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', re.I)
LINK_HREF_RE = re.compile(r'<link[^>]+href=["\']([^"\']+\.(?:js|json)(?:\?[^"\']*)?)["\']', re.I)


def decode_base64_params(raw):
    out = []
    try:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(raw).query)
        for k, vals in q.items():
            for v in vals:
                if len(v) < 20:
                    continue
                if not re.match(r'^[A-Za-z0-9+/=_-]+$', v):
                    continue
                try:
                    norm = v.replace("-", "+").replace("_", "/")
                    pad = norm + "=" * ((4 - len(norm) % 4) % 4)
                    txt = base64.b64decode(pad).decode("utf-8", errors="replace")
                    clean = re.sub(r'[\x00-\x1f]', '', txt)
                    if re.search(r'https?://', clean) or re.search(r'firebase', clean, re.I):
                        out.append({"param": k, "value": clean})
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    continue
    except Exception:
        pass
    return out


def harvest(text, urls, config):
    for re_obj in FIREBASE_PATTERNS:
        for m in re_obj.findall(text):
            norm = re.sub(r'[/.,);"\']+$', '', m).lower()
            urls.add(norm)
    for m in CONFIG_KEY_PATTERN.finditer(text):
        key = KEYMAP.get(m.group(1).lower())
        val = m.group(2).strip().rstrip(",")
        if key and val and key not in config:
            config[key] = val


def count_online_devices(json_text):
    """RTDB dump lo devices count chey."""
    if not json_text or json_text.strip() == "null":
        return 0, 0
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        return 0, 0

    online = 0
    total = 0

    def walk(node):
        nonlocal online, total
        if isinstance(node, dict):
            is_online = (
                node.get("online") is True or
                node.get("isOnline") is True or
                node.get("status") == "online" or
                node.get("state") == "online" or
                node.get("connected") is True
            )
            device_markers = ("deviceId", "device_id", "androidId", "model",
                              "battery", "number", "phone", "sim", "lastSeen")
            if any(k in node for k in device_markers):
                total += 1
                if is_online:
                    online += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return online, total


async def fetch(session, url):
    try:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                return ""
            ctype = resp.headers.get("content-type", "").lower()
            if not any(t in ctype for t in ("text", "javascript", "json", "html")):
                return ""
            raw = await resp.content.read(MAX_HTML)
            return raw.decode("utf-8", errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        return ""


async def fetch_many(session, urls, sem):
    async def one(u):
        async with sem:
            return u, await fetch(session, u)
    return await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)


async def process_target(session, sem, url, follow_assets, scan_rtdb, scan_init):
    urls = set()
    config = {}
    rtdb_status = {}
    init_json = None

    for d in decode_base64_params(url):
        harvest(d["value"], urls, config)

    html = await fetch(session, url)
    if not html:
        return {"state": "err", "error": "fetch failed", "urls": [], "config": {}, "rtdb": {}, "init_json": None}

    harvest(html, urls, config)

    if follow_assets:
        assets = set()
        for re_obj in (SCRIPT_SRC_RE, LINK_HREF_RE):
            for m in re_obj.findall(html):
                try:
                    from urllib.parse import urljoin
                    assets.add(urljoin(url, m))
                except Exception:
                    pass
        asset_list = list(assets)[:MAX_ASSETS]
        if asset_list:
            results = await fetch_many(session, asset_list, sem)
            for r in results:
                if isinstance(r, tuple):
                    _, text = r
                    if text:
                        harvest(text, urls, config)

    if scan_rtdb:
        rtdb_urls = [u for u in urls if "firebaseio.com" in u or "firebasedatabase.app" in u]
        if rtdb_urls:
            checks = await fetch_many(session, [u.rstrip("/") + "/.json?shallow=true" for u in rtdb_urls], sem)
            for i, r in enumerate(checks):
                if not isinstance(r, tuple):
                    continue
                _, text = r
                u = rtdb_urls[i]
                if text and (text.strip().startswith("{") or text.strip() == "null" or text.strip().startswith("[")):
                    full = await fetch(session, u.rstrip("/") + "/.json")
                    online, total = count_online_devices(full)
                    rtdb_status[u] = {
                        "open": True,
                        "reason": "data exposed",
                        "online": online,
                        "total": total,
                    }
                else:
                    rtdb_status[u] = {"open": False, "reason": "denied/unknown", "online": 0, "total": 0}

    if scan_init and config.get("projectId"):
        init_url = f"https://{config['projectId']}.firebaseapp.com/__/firebase/init.json"
        text = await fetch(session, init_url)
        if text:
            try:
                init_json = json.loads(text)
            except json.JSONDecodeError:
                pass

    return {
        "state": "ok",
        "urls": sorted(urls),
        "config": config,
        "rtdb": rtdb_status,
        "init_json": init_json,
    }


async def run_all(targets, follow_assets, scan_rtdb, scan_init):
    sem = asyncio.Semaphore(CONCURRENCY)
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY * 2, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout,
        headers={"User-Agent": UA, "Accept": "*/*"},
    ) as session:
        tasks = [
            process_target(session, sem, t, follow_assets, scan_rtdb, scan_init)
            for t in targets
        ]
        return await asyncio.gather(*tasks)


@app.route("/")
def index():
    return send_from_directory(".", "extractor.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    data = request.get_json(silent=True) or {}
    targets = data.get("targets", [])
    if not isinstance(targets, list) or not targets:
        return jsonify({"error": "no targets"}), 400
    follow_assets = bool(data.get("follow_assets", True))
    scan_rtdb = bool(data.get("scan_rtdb", True))
    scan_init = bool(data.get("scan_init", True))

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(
            run_all(targets, follow_assets, scan_rtdb, scan_init)
        )
        loop.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "results": [
            {"source": t, **r} for t, r in zip(targets, results)
        ]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[*] Kr_Url_Extractor backend — http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
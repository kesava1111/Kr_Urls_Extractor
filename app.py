# ============================================================
# Kr Firebase Extractor - Backend v4.2
# Firebase Active/Inactive + Response stats
# ============================================================

import re
import os
import time
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=".")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# HOME - index.html serve cheyyi
# ============================================================
@app.route("/")
def home():
    for name in ("index.html", "extractor.html", "Extractor.htm"):
        try:
            return send_from_directory(".", name)
        except Exception:
            continue
    return "index.html file dorakaledu. Same folder lo pettu."


# ============================================================
# FIREBASE EXTRACTOR
# ============================================================
def extract_firebase(html):
    firebase_urls = set()
    config = {}

    patterns_url = [
        r'https?://[a-zA-Z0-9\-_]+\.firebaseio\.com',
        r'https?://[a-zA-Z0-9\-_]+\.firebaseapp\.com',
        r'https?://[a-zA-Z0-9\-_]+\.firebasedatabase\.app',
        r'https?://[a-zA-Z0-9\-_]+\.web\.app',
        r'https?://firestore\.googleapis\.com/v1/projects/[a-zA-Z0-9\-_]+',
    ]
    for pat in patterns_url:
        for m in re.findall(pat, html):
            firebase_urls.add(m)

    config_keys = [
        "apiKey", "authDomain", "databaseURL", "projectId",
        "storageBucket", "messagingSenderId", "appId", "measurementId",
    ]

    for key in config_keys:
        pattern = r'"' + key + r'"\s*:\s*"([^"]+)"'
        m = re.search(pattern, html)
        if m:
            config[key] = m.group(1)
            if key == "databaseURL":
                firebase_urls.add(m.group(1))
            if key == "authDomain":
                firebase_urls.add("https://" + m.group(1))

    for key in config_keys:
        if key not in config:
            pattern = r"'" + key + r"'\s*:\s*'([^']+)'"
            m = re.search(pattern, html)
            if m:
                config[key] = m.group(1)

    return sorted(firebase_urls), config


# ============================================================
# CHECK FIREBASE URL STATUS
# ============================================================
def check_firebase_status(url):
    result = {
        "status": "unknown",
        "response_time": 0,
        "data_size": 0,
        "keys": 0,
        "error": ""
    }

    if not (".firebaseio.com" in url or ".firebasedatabase.app" in url):
        result["status"] = "n/a"
        result["error"] = "Not a realtime DB"
        return result

    test_url = url.rstrip("/") + "/.json"

    try:
        start = time.time()
        r = requests.get(test_url, headers=HEADERS, timeout=8)
        elapsed = (time.time() - start) * 1000

        result["response_time"] = int(elapsed)
        result["data_size"] = len(r.content)

        if r.status_code == 200:
            text = r.text.strip()
            if text == "null" or text == "":
                result["status"] = "active"
                result["keys"] = 0
            else:
                result["status"] = "active"
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        result["keys"] = len(data)
                    elif isinstance(data, list):
                        result["keys"] = len(data)
                    else:
                        result["keys"] = 1
                except Exception:
                    result["keys"] = 0
        elif r.status_code in (401, 403):
            result["status"] = "active"
            result["error"] = "Auth required"
        elif r.status_code == 404:
            result["status"] = "inactive"
            result["error"] = "Not found"
        else:
            result["status"] = "active"
            result["error"] = "HTTP " + str(r.status_code)

    except requests.exceptions.Timeout:
        result["status"] = "inactive"
        result["error"] = "Timeout"
    except requests.exceptions.ConnectionError:
        result["status"] = "inactive"
        result["error"] = "Connection failed"
    except Exception as e:
        result["status"] = "inactive"
        result["error"] = str(e)[:60]

    return result


# ============================================================
# API - /extract?url=...
# ============================================================
@app.route("/extract")
def extract():
    url = request.args.get("url", "").strip()

    if not url:
        return jsonify({
            "error": "No URL provided",
            "firebase_urls": [],
            "config": {},
            "statuses": {}
        }), 400

    if not url.startswith("http"):
        url = "https://" + url

    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
    except Exception as e:
        return jsonify({
            "error": f"Fetch failed: {str(e)[:80]}",
            "firebase_urls": [],
            "config": {},
            "statuses": {}
        }), 200

    firebase_urls, config = extract_firebase(res.text)

    statuses = {}
    for fb_url in firebase_urls:
        statuses[fb_url] = check_firebase_status(fb_url)

    return jsonify({
        "url": url,
        "firebase_urls": firebase_urls,
        "config": config,
        "statuses": statuses
    })


# ============================================================
# HEALTH CHECK
# ============================================================
@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print(f" Kr Firebase Extractor running on port {port}")
    print(f" Open: http://127.0.0.1:{port}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
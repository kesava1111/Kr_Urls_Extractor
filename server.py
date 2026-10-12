# ============================================================
# server.py - Flask backend for Extractor.htm
# HTML + JS render + Online count + Auto selectors
# ============================================================

import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=".")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ============================================================
# HOME - Extractor.htm serve cheyyi
# ============================================================
@app.route("/")
def home():
    for name in ("Extractor.htm", "Extractor.html", "index.html"):
        try:
            return send_from_directory(".", name)
        except Exception:
            continue
    return "Extractor.htm file dorakaledu. Same folder lo pettu."


# ============================================================
# ONLINE COUNT - smart auto detect
# ============================================================
def get_online_count(soup, raw_html=""):
    selectors = [
        "#online-count", ".online-count", ".onlineCount",
        "#online", ".online", ".online-users", ".online_users",
        "[class*=online]", "[id*=online]",
        "[class*=active-user]", "[class*=activeUser]",
        "[class*=users-online]",
    ]
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            m = re.search(r"(\d[\d,\.]*)", el.get_text())
            if m:
                return int(m.group(1).replace(",", "").replace(".", ""))

    text = soup.get_text(" ", strip=True)
    patterns = [
        r"(\d[\d,]*)\s*(?:users?\s*)?online",
        r"online\s*[:\-]?\s*(\d[\d,]*)",
        r"(\d[\d,]*)\s*active\s*(?:users?)?",
        r"(\d[\d,]*)\s*people\s*(?:are\s*)?online",
        r"currently\s*(\d[\d,]*)",
        r"(\d[\d,]*)\s*members?\s*online",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))

    if raw_html:
        for pat in patterns:
            m = re.search(pat, raw_html, re.IGNORECASE)
            if m:
                return int(m.group(1).replace(",", ""))

    return 0


# ============================================================
# AUTO SELECTOR - item elements kanukko
# ============================================================
def auto_find_items(soup):
    candidates = [
        "article",
        "div.item", "div.items", "div.card", "div.product",
        "div.news-item", "div.post", "div.entry", "div.list-item",
        "li.item", "li.product", "li.post",
        "div[class*=item]", "div[class*=card]",
        "div[class*=product]", "div[class*=post]",
        "div[class*=article]", "div[class*=news]",
        "li[class*=item]", "li[class*=product]",
        "[data-item]", "[data-id]",
    ]
    best = []
    for sel in candidates:
        try:
            found = soup.select(sel)
            if len(found) > len(best):
                best = found
        except Exception:
            continue

    if not best:
        links = soup.find_all("a", href=True)
        links = [a for a in links if len(a.get_text(strip=True)) > 15]
        best = links[:50]

    return best


# ============================================================
# PARSE ITEMS
# ============================================================
def parse_items(items, base_url):
    data = []
    seen = set()
    base_match = re.match(r"(https?://[^/]+)", base_url)
    base = base_match.group(1) if base_match else ""

    for it in items:
        title = it.get_text(" ", strip=True)
        if not title or len(title) < 5:
            continue
        title = title[:150]

        link_tag = it.find("a", href=True) if it.name != "a" else it
        link = link_tag["href"] if link_tag else ""

        if link.startswith("/") and base:
            link = base + link
        elif link and not link.startswith("http") and base:
            link = base + "/" + link.lstrip("/")

        key = (title, link)
        if key in seen:
            continue
        seen.add(key)
        data.append({"title": title, "link": link})

    return data


# ============================================================
# METHOD 1: requests + BeautifulSoup
# ============================================================
def extract_html(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
    except Exception as e:
        return [], 0, f"Request fail: {e}"

    soup = BeautifulSoup(res.text, "html.parser")
    items = auto_find_items(soup)
    data = parse_items(items, url)
    online = get_online_count(soup, res.text)
    return data, online, ""


# ============================================================
# METHOD 2: Playwright (JS-rendered)
# ============================================================
def extract_js(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 0, "Playwright install ledu"

    data, online = [], 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=30000)

            try:
                page.wait_for_selector(
                    "[class*=online], [id*=online]", timeout=5000
                )
            except Exception:
                pass

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            items = auto_find_items(soup)
            data = parse_items(items, url)
            online = get_online_count(soup, html)
            browser.close()
    except Exception as e:
        return [], 0, f"Playwright error: {e}"

    return data, online, ""


# ============================================================
# API - /extract?url=...
# ============================================================
@app.route("/extract")
def extract():
    url = request.args.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL pettu", "items": [], "online": 0}), 400

    if not url.startswith("http"):
        url = "https://" + url

    data, online, err1 = extract_html(url)

    if not data:
        data2, online2, err2 = extract_js(url)
        if data2:
            data, online = data2, online2
        else:
            return jsonify({
                "error": "No data extracted. " + (err1 or err2 or "Selectors match avvatledu"),
                "items": [],
                "online": 0
            }), 200

    return jsonify({
        "url": url,
        "count": len(data),
        "online": online,
        "items": data
    })


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("🚀 Server started")
    print("🌐 Open: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
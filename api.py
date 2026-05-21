"""
api.py — LudoPrice Backend (Railway)
=====================================
API Flask para añadir juegos manualmente por URL.
Scrapea la página del producto, guarda en games.db
y hace push del JSON actualizado al repo de GitHub.

Endpoints:
  POST /add-game   { "url": "https://mathom.es/..." }
  GET  /health     → {"status": "ok"}
"""

import os
import re
import json
import time
import sqlite3
import logging
import subprocess
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS

# ── Config ────────────────────────────────────────────────────────────────────
PORT         = int(os.environ.get("PORT", 8080))
DB_PATH      = Path("/tmp/games.db")
JSON_PATH    = Path("/tmp/games_data.json")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "SullivanWhite/ScanPrice2.0")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}

SUPPORTED = {
    "mathom.es":          "mathom",
    "goblintrader.es":    "goblintrader",
    "dungeonmarvels.com": "dungeonmarvels",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # permite llamadas desde GitHub Pages

# ── DB ────────────────────────────────────────────────────────────────────────
def download_db_from_github():
    """Descarga la games.db actual del repo de GitHub a /tmp."""
    if not GITHUB_TOKEN:
        log.warning("Sin GITHUB_TOKEN — no se puede descargar la DB")
        return False
    import base64
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/games.db"
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}
    r = requests.get(api_url, headers=headers)
    if r.status_code != 200:
        log.error("No se pudo descargar games.db: %s", r.status_code)
        return False
    data = r.json()
    db_bytes = base64.b64decode(data["content"])
    DB_PATH.write_bytes(db_bytes)
    log.info("games.db descargada del repo (%d bytes)", len(db_bytes))
    return True


def upload_db_to_github():
    """Sube la games.db actualizada al repo de GitHub."""
    if not GITHUB_TOKEN:
        return False
    import base64
    content_b64 = base64.b64encode(DB_PATH.read_bytes()).decode()
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/games.db"
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}
    r = requests.get(api_url, headers=headers)
    sha = r.json().get("sha", "") if r.status_code == 200 else ""
    payload = {
        "message": f"chore: update db via API {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(api_url, headers=headers, json=payload)
    ok = r.status_code in (200, 201)
    log.info("Upload games.db a GitHub: %s", "OK" if ok else f"ERROR {r.status_code}")
    return ok


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source     TEXT    NOT NULL,
            name       TEXT    NOT NULL,
            url        TEXT    NOT NULL UNIQUE,
            first_seen TEXT    NOT NULL,
            is_new     INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id        INTEGER NOT NULL,
            price          REAL,
            original_price REAL,
            discount_pct   REAL,
            scraped_at     TEXT NOT NULL,
            FOREIGN KEY (game_id) REFERENCES games(id)
        );
    """)
    conn.commit()
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────
def detect_source(url: str) -> str | None:
    for domain, source in SUPPORTED.items():
        if domain in url:
            return source
    return None


def limpiar_precio(texto) -> float | None:
    if not texto:
        return None
    texto = re.sub(r"[^\d,\.]", "", str(texto).strip())
    texto = texto.replace(",", ".")
    partes = texto.split(".")
    if len(partes) > 2:
        texto = "".join(partes[:-1]) + "." + partes[-1]
    try:
        v = float(texto)
        return v if v > 0 else None
    except ValueError:
        return None


def scrape_product_page(url: str, source: str) -> dict | None:
    """Extrae nombre, precio actual y precio original de una página de producto."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning("Error fetching %s: %s", url, e)
        return None

    # ── Nombre ────────────────────────────────────────────────────────────────
    name = None
    for sel in [
        ("h1", {"class": re.compile(r"product.name|product.title", re.I)}),
        ("h1", {}),
        ("h2", {"class": re.compile(r"product.title", re.I)}),
    ]:
        tag = soup.find(sel[0], sel[1])
        if tag:
            name = tag.get_text(strip=True)
            break

    if not name:
        title = soup.find("title")
        name = title.get_text(strip=True).split("|")[0].strip() if title else None

    if not name:
        return None

    # ── Precio actual ─────────────────────────────────────────────────────────
    precio = None
    # Intentar con current-price-value primero
    tag = soup.find("span", class_="current-price-value")
    if tag:
        precio = limpiar_precio(tag.get("content") or tag.get_text())

    # Fallback: span.price exacto
    if not precio:
        for span in soup.find_all("span"):
            clases = span.get("class", [])
            if "price" in clases and "regular-price" not in clases:
                precio = limpiar_precio(span.get_text())
                if precio:
                    break

    # ── Precio original ───────────────────────────────────────────────────────
    orig = None
    orig_tag = soup.find("span", class_="regular-price")
    if orig_tag:
        orig = limpiar_precio(orig_tag.get_text())

    # ── Descuento ─────────────────────────────────────────────────────────────
    disc = None
    disc_tag = soup.find("span", class_="discount-percentage")
    if disc_tag:
        m = re.search(r"(\d+)", disc_tag.get_text())
        disc = float(m.group(1)) if m else None
    elif orig and precio and orig > precio:
        disc = round((1 - precio / orig) * 100, 1)

    return {
        "source":         source,
        "name":           name,
        "url":            url,
        "price":          precio,
        "original_price": orig,
        "discount_pct":   disc,
        "scraped_at":     datetime.now().isoformat(),
    }


def save_game(conn, data: dict) -> tuple[int, bool]:
    cur = conn.cursor()
    cur.execute("SELECT id FROM games WHERE url = ?", (data["url"],))
    row = cur.fetchone()
    if row:
        return row["id"], False
    cur.execute(
        "INSERT INTO games (source, name, url, first_seen, is_new) VALUES (?,?,?,?,1)",
        (data["source"], data["name"], data["url"], data["scraped_at"]),
    )
    conn.commit()
    return cur.lastrowid, True


def save_price(conn, game_id: int, data: dict):
    cur = conn.execute(
        "SELECT price, original_price FROM price_history WHERE game_id=? ORDER BY scraped_at DESC LIMIT 1",
        (game_id,),
    )
    last = cur.fetchone()
    if last and last["price"] == data["price"] and last["original_price"] == data["original_price"]:
        return
    conn.execute(
        "INSERT INTO price_history (game_id, price, original_price, discount_pct, scraped_at) VALUES (?,?,?,?,?)",
        (game_id, data["price"], data["original_price"], data["discount_pct"], data["scraped_at"]),
    )
    conn.commit()


def rebuild_json(conn):
    """Reconstruye games_data.json desde la DB y lo guarda en /data/."""
    cur = conn.execute("""
        SELECT g.id, g.source, g.name, g.url, g.first_seen, g.is_new,
               ph.price, ph.original_price, ph.discount_pct, ph.scraped_at
        FROM games g
        LEFT JOIN price_history ph ON ph.id = (
            SELECT id FROM price_history WHERE game_id = g.id
            ORDER BY scraped_at DESC LIMIT 1
        )
        ORDER BY g.is_new DESC, g.first_seen DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]

    # Historial por game_id
    def get_history(game_id):
        h = conn.execute(
            "SELECT price, original_price, discount_pct, scraped_at FROM price_history WHERE game_id=? ORDER BY scraped_at ASC",
            (game_id,),
        )
        return [{"price": r["price"], "original_price": r["original_price"],
                 "discount_pct": r["discount_pct"], "date": r["scraped_at"][:10]}
                for r in h.fetchall()]

    # Agrupar por nombre normalizado
    import unicodedata
    def norm(s):
        s = unicodedata.normalize("NFD", s.lower())
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z0-9]", "", s)

    grupos, orden = {}, []
    for r in rows:
        key = norm(r["name"])
        if key not in grupos:
            orden.append(key)
            grupos[key] = {"name": r["name"], "is_new": r["is_new"],
                           "first_seen": r["first_seen"], "prices": []}
        else:
            if r["is_new"]:
                grupos[key]["is_new"] = 1
        grupos[key]["prices"].append({
            "source": r["source"], "url": r["url"],
            "price": r["price"], "original_price": r["original_price"],
            "discount_pct": r["discount_pct"], "scraped_at": r["scraped_at"],
            "history": get_history(r["id"]),
        })

    games = [grupos[k] for k in orden]
    data  = {"generated_at": datetime.now().isoformat(),
             "total": len(games), "games": games}
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("JSON reconstruido: %d juegos", len(games))
    return len(games)


def push_json_to_github():
    """Sube games_data.json al repo de GitHub via API."""
    if not GITHUB_TOKEN:
        log.warning("Sin GITHUB_TOKEN — no se puede hacer push")
        return False

    content = JSON_PATH.read_bytes()
    import base64
    b64 = base64.b64encode(content).decode()

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/games_data.json"
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}

    # Obtener SHA actual del archivo
    r = requests.get(api_url, headers=headers)
    sha = r.json().get("sha", "") if r.status_code == 200 else ""

    payload = {
        "message": f"chore: add game via API {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": b64,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(api_url, headers=headers, json=payload)
    if r.status_code in (200, 201):
        log.info("JSON pushed a GitHub OK")
        return True
    else:
        log.error("Error push GitHub: %s %s", r.status_code, r.text[:200])
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/add-game", methods=["POST"])
def add_game():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "Falta el campo 'url'"}), 400

    url = data["url"].strip()
    source = detect_source(url)
    if not source:
        return jsonify({
            "error": "URL no reconocida. Tiendas soportadas: Mathom, GoblinTrader, Dungeon Marvels"
        }), 400

    log.info("Añadiendo juego: %s (%s)", url, source)

    # Scrape la página del producto
    product = scrape_product_page(url, source)
    if not product:
        return jsonify({"error": "No se pudo extraer información de la URL"}), 422

    if not product.get("price"):
        return jsonify({"error": f"Juego encontrado ({product['name']}) pero sin precio visible"}), 422

    # Descargar DB actual del repo
    download_db_from_github()

    # Guardar en DB
    conn = get_db()
    game_id, is_new = save_game(conn, product)
    save_price(conn, game_id, product)
    total = rebuild_json(conn)
    conn.close()

    # Subir DB y JSON actualizados a GitHub
    upload_db_to_github()
    pushed = push_json_to_github()

    return jsonify({
        "ok":      True,
        "is_new":  is_new,
        "name":    product["name"],
        "source":  source,
        "price":   product["price"],
        "original_price": product["original_price"],
        "discount_pct":   product["discount_pct"],
        "pushed_to_github": pushed,
        "total_games": total,
    })


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)

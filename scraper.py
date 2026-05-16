"""
ScanPrice Board Games Scraper
==============================
Scrapes Mathom y GoblinTrader (chollos), guarda en SQLite,
detecta juegos nuevos y exporta games_data.json para el dashboard.

Dependencias: pip install requests beautifulsoup4 lxml
Ejecutar:     python scraper.py
"""

import re
import json
import time
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Rutas ────────────────────────────────────────────────────────────────────
DB_PATH     = Path(__file__).parent / "games.db"
LOG_PATH    = Path(__file__).parent / "scraper.log"
OUTPUT_JSON = Path(__file__).parent / "games_data.json"

# ┌─────────────────────────────────────────────────────────────────────────┐
# │  DUNGEON MARVELS — URLs a scrapear                                     │
# │                                                                        │
# │  DUNGEON_MARVELS_CATALOGO: categoría fija de juegos de tablero.        │
# │  No tocar — nunca cambia.                                              │
# │                                                                        │
# │  DUNGEON_MARVELS_PROMO: campaña mensual de ofertas.                    │
# │  Cámbiala cuando estrenen una nueva promo. Entra en dungeonmarvels.com,│
# │  haz clic en el banner de la promo activa y copia la URL.              │
# │  Ponla a None si no hay campaña activa o no te interesa scrapearla.    │
# │                                                                        │
# │  Ejemplos de promo:                                                    │
# │    "https://dungeonmarvels.com/1966-liquidacion-mayo"  ← mayo 2026     │
# │    "https://dungeonmarvels.com/217-super-ofertas"      ← permanente    │
# │    None                                                ← desactivada   │
# └─────────────────────────────────────────────────────────────────────────┘
# DUNGEON_MARVELS_CATALOGO ya no se usa — demasiados productos
# ↓ Cambia esta URL cada vez que haya una nueva campaña
DUNGEON_MARVELS_PROMO = "https://dungeonmarvels.com/1968-liquidacion-mayo-juegos"

# ── HTTP ─────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
PAUSA = 1.5   # segundos entre peticiones

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Modelo ───────────────────────────────────────────────────────────────────
@dataclass
class Game:
    source:         str
    name:           str
    url:            str
    price:          Optional[float]
    original_price: Optional[float]
    discount_pct:   Optional[float]
    scraped_at:     str = ""

    def __post_init__(self):
        if not self.scraped_at:
            self.scraped_at = datetime.now().isoformat()


# ── Base de datos ─────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
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
        CREATE INDEX IF NOT EXISTS idx_games_url    ON games(url);
        CREATE INDEX IF NOT EXISTS idx_history_game ON price_history(game_id);
    """)
    conn.commit()
    return conn


def upsert_game(conn: sqlite3.Connection, game: Game) -> tuple[int, bool]:
    """Devuelve (game_id, is_new). is_new=True solo la primera vez que aparece la URL."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM games WHERE url = ?", (game.url,))
    row = cur.fetchone()
    if row:
        return row["id"], False
    cur.execute(
        "INSERT INTO games (source, name, url, first_seen, is_new) VALUES (?,?,?,?,1)",
        (game.source, game.name, game.url, game.scraped_at),
    )
    conn.commit()
    return cur.lastrowid, True


def record_price(conn: sqlite3.Connection, game_id: int, game: Game):
    """Solo guarda si el precio cambió respecto a la última entrada. Evita duplicados."""
    cur = conn.execute(
        """SELECT price, original_price FROM price_history
           WHERE game_id = ? ORDER BY scraped_at DESC LIMIT 1""",
        (game_id,),
    )
    last = cur.fetchone()
    if last and last["price"] == game.price and last["original_price"] == game.original_price:
        return  # precio idéntico, no guardar
    conn.execute(
        """INSERT INTO price_history (game_id, price, original_price, discount_pct, scraped_at)
           VALUES (?,?,?,?,?)""",
        (game_id, game.price, game.original_price, game.discount_pct, game.scraped_at),
    )
    conn.commit()


def mark_old(conn: sqlite3.Connection, source: str):
    """Al terminar una fuente, los juegos ya vistos dejan de marcarse como nuevos."""
    conn.execute("UPDATE games SET is_new = 0 WHERE source = ? AND is_new = 1", (source,))
    conn.commit()


def all_games_latest(conn: sqlite3.Connection) -> list[dict]:
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
    return [dict(r) for r in cur.fetchall()]


# ── Helpers ───────────────────────────────────────────────────────────────────
def limpiar_precio(texto) -> Optional[float]:
    """Convierte '29,95 €', '29.95' o el attr content='29.95' → 29.95"""
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


def fetch(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    time.sleep(PAUSA)
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            log.warning("HTTP %s → %s", r.status_code, url)
            return None
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning("Error fetching %s: %s", url, e)
        return None


def tiene_productos(soup: BeautifulSoup) -> bool:
    return bool(soup.find("article", class_=re.compile(r"product")))


# ── Scraper Mathom ────────────────────────────────────────────────────────────
# Selectores verificados desde el HTML real (debug2):
#
#   article.product-miniature.js-product-miniature  → bloque de producto
#   a.product-thumbnail[href]                        → URL del producto
#   h2.product-title > a                             → nombre (texto del tag)
#   span.price                                       → precio actual ("31,49 €")
#   span.regular-price                               → precio original tachado
#   span.discount-percentage                         → badge "-10%"
#
# IMPORTANTE: el fallback genérico class*=price pillaba span.regular-price
# antes que span.price — por eso el precio salía mal. Ahora se busca
# span.price con clase exacta primero, y span.regular-price por separado.

def parse_mathom_article(art) -> Optional[Game]:
    # ── URL ───────────────────────────────────────────────────────────────────
    url_tag = art.find("a", class_="product-thumbnail")
    if not url_tag:
        return None
    url = url_tag.get("href", "").strip()
    if not url:
        return None

    # ── Nombre: h2.product-title > a (texto, no atributo title) ──────────────
    titulo_tag = art.find("h2", class_="product-title")
    if titulo_tag:
        a = titulo_tag.find("a")
        nombre = a.get_text(strip=True) if a else titulo_tag.get_text(strip=True)
    else:
        nombre = url_tag.get("title", "").strip() or url_tag.get_text(strip=True)
    if not nombre:
        return None

    # ── Precio original tachado: span.regular-price ───────────────────────────
    orig_tag = art.find("span", class_="regular-price")
    orig     = limpiar_precio(orig_tag.get_text()) if orig_tag else None

    # ── Precio actual: span.price (clase exacta, no regex) ───────────────────
    # Buscar span cuya lista de clases contenga "price" exacto
    precio = None
    for span in art.find_all("span"):
        clases = span.get("class", [])
        if "price" in clases and "regular-price" not in clases:
            precio = limpiar_precio(span.get_text())
            if precio:
                break

    # ── Descuento: span.discount-percentage ───────────────────────────────────
    disc_tag = art.find("span", class_="discount-percentage")
    disc = None
    if disc_tag:
        m = re.search(r"(\d+)", disc_tag.get_text())
        disc = float(m.group(1)) if m else None
    elif orig and precio and orig > precio:
        disc = round((1 - precio / orig) * 100, 1)

    return Game(
        source="mathom",
        name=nombre,
        url=url,
        price=precio,
        original_price=orig,
        discount_pct=disc,
    )


def _page_fingerprint(arts) -> frozenset:
    """Conjunto de URLs de los artículos de una página — sirve para detectar duplicados."""
    return frozenset(
        art.find("a", class_="product-thumbnail").get("href", "")
        for art in arts
        if art.find("a", class_="product-thumbnail")
    )


def scrape_mathom(conn: sqlite3.Connection, session: requests.Session) -> int:
    BASE      = "https://mathom.es/es/244-juegos-de-tablero"
    total_new = 0
    seen_fingerprints: set[frozenset] = set()
    log.info("▶  Mathom — iniciando scraping...")

    for pag in range(1, 60):
        url  = BASE if pag == 1 else f"{BASE}?page={pag}"
        log.info("   Página %d → %s", pag, url)
        soup = fetch(url, session)

        if not soup or not tiene_productos(soup):
            log.info("   Sin productos en página %d. Fin de paginación.", pag)
            break

        # Buscar dentro del contenedor confirmado #js-product-list
        container = soup.find(id="js-product-list") or soup
        arts = container.find_all("article", class_="product-miniature")

        if not arts:
            log.info("   Sin artículos en página %d. Fin de paginación.", pag)
            break

        # Detectar bucle: Mathom devuelve la pág 1 cuando se pide una pág inexistente
        fp = _page_fingerprint(arts)
        if fp in seen_fingerprints:
            log.info("   Página %d idéntica a una anterior — fin de paginación real.", pag)
            break
        seen_fingerprints.add(fp)

        games_found = 0
        for art in arts:
            game = parse_mathom_article(art)
            if not game:
                continue
            game_id, is_new = upsert_game(conn, game)
            record_price(conn, game_id, game)
            games_found += 1
            if is_new:
                total_new += 1
                log.info("   ✨ NUEVO: %s (%.2f €)", game.name, game.price or 0)

        log.info("   → %d juegos encontrados", games_found)

    mark_old(conn, "mathom")
    log.info("✅ Mathom completado. %d juegos nuevos.", total_new)
    return total_new


# ── Scraper GoblinTrader ──────────────────────────────────────────────────────
# Sección "Chollos Juegos de Mesa" — mismo PrestaShop, mismos selectores.
# GoblinTrader a veces usa <div class="js-product-miniature"> en lugar de <article>.

BASE_GOBLIN = "https://www.goblintrader.es"


def parse_goblin_article(art) -> Optional[Game]:
    # ── Nombre y URL ──────────────────────────────────────────────────────────
    link = art.find("a", class_=re.compile(r"product.thumbnail|product.title", re.I))
    if not link:
        link = art.find("a", href=True)
    if not link:
        return None

    nombre = link.get("title", "").strip() or link.get_text(strip=True)
    url    = link.get("href", "")
    if not url.startswith("http"):
        url = BASE_GOBLIN + url
    if not nombre or not url:
        return None

    # ── Precio actual ─────────────────────────────────────────────────────────
    precio_tag = art.find("span", class_="current-price-value")
    if precio_tag:
        precio = limpiar_precio(precio_tag.get("content") or precio_tag.get_text())
    else:
        # Fallback: primer span con class "price" (excluyendo old/regular)
        for span in art.find_all("span", class_=re.compile(r"price")):
            cls = " ".join(span.get("class", []))
            if "regular" not in cls and "old" not in cls:
                precio = limpiar_precio(span.get_text())
                if precio:
                    break
        else:
            precio = None

    # ── Precio original ───────────────────────────────────────────────────────
    orig_tag = art.find("span", class_=re.compile(r"regular.price|old.price", re.I))
    orig     = limpiar_precio(orig_tag.get_text()) if orig_tag else None

    # ── Descuento ─────────────────────────────────────────────────────────────
    disc_tag = art.find("span", class_=re.compile(r"discount.percentage", re.I))
    disc = None
    if disc_tag:
        m = re.search(r"(\d+)", disc_tag.get_text())
        disc = float(m.group(1)) if m else None
    elif orig and precio and orig > precio:
        disc = round((1 - precio / orig) * 100, 1)

    return Game(
        source="goblintrader",
        name=nombre,
        url=url,
        price=precio,
        original_price=orig,
        discount_pct=disc,
    )


def scrape_goblintrader(conn: sqlite3.Connection, session: requests.Session) -> int:
    BASE      = f"{BASE_GOBLIN}/es/9910121-Chollos-Juegos-de-Mesa"
    total_new = 0
    log.info("▶  GoblinTrader — iniciando scraping...")

    for pag in range(1, 30):
        url  = BASE if pag == 1 else f"{BASE}?page={pag}"
        log.info("   Página %d → %s", pag, url)
        soup = fetch(url, session)

        if not soup:
            break

        # GoblinTrader usa div.js-product-miniature o article.product-miniature
        arts = soup.find_all(
            ["article", "div"],
            class_=re.compile(r"product.miniature|js.product.miniature", re.I),
        )
        if not arts:
            arts = soup.find_all("article", class_=re.compile(r"product"))
        if not arts:
            log.info("   Sin productos en página %d. Fin de paginación.", pag)
            break

        games_found = 0
        for art in arts:
            game = parse_goblin_article(art)
            if not game:
                continue
            game_id, is_new = upsert_game(conn, game)
            record_price(conn, game_id, game)
            games_found += 1
            if is_new:
                total_new += 1
                log.info("   ✨ NUEVO: %s (%.2f €)", game.name, game.price or 0)

        log.info("   → %d juegos encontrados", games_found)
        if games_found == 0:
            break

    mark_old(conn, "goblintrader")
    log.info("✅ GoblinTrader completado. %d juegos nuevos.", total_new)
    return total_new


# ── Scraper Dungeon Marvels ───────────────────────────────────────────────────
# También PrestaShop. Mismos selectores que Mathom.
# La URL de la campaña activa se configura arriba en DUNGEON_MARVELS_URL.

def parse_dungeon_article(art) -> Optional[Game]:
    # ── URL ───────────────────────────────────────────────────────────────────
    url_tag = art.find("a", class_="product-thumbnail")
    if not url_tag:
        url_tag = art.find("a", href=re.compile(r"dungeonmarvels\.com"))
    if not url_tag:
        url_tag = art.find("a", href=True)
    if not url_tag:
        return None
    url = url_tag.get("href", "").strip()
    if not url.startswith("http"):
        url = "https://dungeonmarvels.com" + url
    if not url:
        return None

    # ── Nombre ────────────────────────────────────────────────────────────────
    titulo_tag = art.find("h2", class_="product-title") or art.find("h3", class_="product-title")
    if titulo_tag:
        a = titulo_tag.find("a")
        nombre = a.get_text(strip=True) if a else titulo_tag.get_text(strip=True)
    else:
        nombre = url_tag.get("title", "").strip() or url_tag.get_text(strip=True)
    if not nombre:
        return None

    # ── Precio original tachado ───────────────────────────────────────────────
    orig_tag = art.find("span", class_="regular-price")
    orig     = limpiar_precio(orig_tag.get_text()) if orig_tag else None

    # ── Precio actual (span con clase exacta "price") ─────────────────────────
    precio = None
    for span in art.find_all("span"):
        clases = span.get("class", [])
        if "price" in clases and "regular-price" not in clases:
            precio = limpiar_precio(span.get_text())
            if precio:
                break

    # ── Descuento ─────────────────────────────────────────────────────────────
    disc_tag = art.find("span", class_="discount-percentage")
    disc = None
    if disc_tag:
        m = re.search(r"(\d+)", disc_tag.get_text())
        disc = float(m.group(1)) if m else None
    elif orig and precio and orig > precio:
        disc = round((1 - precio / orig) * 100, 1)

    return Game(
        source="dungeonmarvels",
        name=nombre,
        url=url,
        price=precio,
        original_price=orig,
        discount_pct=disc,
    )


def _scrape_dungeon_url(conn: sqlite3.Connection, session: requests.Session,
                        base_url: str, label: str) -> int:
    """Scrapea una URL de Dungeon Marvels con paginación. Devuelve nº de juegos nuevos."""
    total_new = 0
    seen_fps: set = set()
    base_url = base_url.rstrip("/")

    for pag in range(1, 60):
        url  = base_url if pag == 1 else f"{base_url}?page={pag}"
        log.info("   [%s] Página %d → %s", label, pag, url)
        soup = fetch(url, session)

        if not soup or not tiene_productos(soup):
            log.info("   [%s] Sin productos en página %d. Fin.", label, pag)
            break

        container = soup.find(id="js-product-list") or soup
        arts = container.find_all("article", class_="product-miniature")
        if not arts:
            arts = container.find_all("article", class_=re.compile(r"product"))
        if not arts:
            break

        fp = frozenset(
            a.find("a", class_="product-thumbnail").get("href", "")
            for a in arts if a.find("a", class_="product-thumbnail")
        )
        if fp in seen_fps:
            log.info("   [%s] Página %d idéntica — fin de paginación real.", label, pag)
            break
        seen_fps.add(fp)

        games_found = 0
        for art in arts:
            game = parse_dungeon_article(art)
            if not game:
                continue
            game_id, is_new = upsert_game(conn, game)
            record_price(conn, game_id, game)
            games_found += 1
            if is_new:
                total_new += 1
                log.info("   ✨ NUEVO: %s (%.2f €)", game.name, game.price or 0)

        log.info("   [%s] → %d juegos encontrados", label, games_found)

    return total_new


def scrape_dungeonmarvels(conn: sqlite3.Connection, session: requests.Session) -> int:
    log.info("▶  Dungeon Marvels — iniciando scraping...")
    total_new = 0

    # Campaña de ofertas del mes — cambia DUNGEON_MARVELS_PROMO arriba cuando cambie
    if DUNGEON_MARVELS_PROMO:
        log.info("   Promo activa: %s", DUNGEON_MARVELS_PROMO)
        total_new += _scrape_dungeon_url(conn, session, DUNGEON_MARVELS_PROMO, "promo")
    else:
        log.info("   Sin promo activa configurada.")

    mark_old(conn, "dungeonmarvels")
    log.info("✅ Dungeon Marvels completado. %d juegos nuevos.", total_new)
    return total_new


# ── Agrupación de duplicados ─────────────────────────────────────────────────
def _normalizar(nombre: str) -> str:
    """Clave de dedup: minúsculas, sin acentos, solo alfanumérico."""
    import unicodedata
    s = unicodedata.normalize("NFD", nombre.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def group_games(rows: list[dict]) -> list[dict]:
    """
    Agrupa filas con el mismo nombre normalizado.
    Cada juego queda con 'prices': lista de {source, url, price, original_price, discount_pct}.
    """
    grupos: dict[str, dict] = {}
    orden: list[str] = []

    for r in rows:
        key = _normalizar(r["name"])
        if key not in grupos:
            orden.append(key)
            grupos[key] = {
                "name":       r["name"],
                "is_new":     r["is_new"],
                "first_seen": r["first_seen"],
                "prices":     [],
            }
        else:
            if r["is_new"]:
                grupos[key]["is_new"] = 1

        grupos[key]["prices"].append({
            "source":         r["source"],
            "url":            r["url"],
            "price":          r["price"],
            "original_price": r["original_price"],
            "discount_pct":   r["discount_pct"],
            "scraped_at":     r["scraped_at"],
        })

    return [grupos[k] for k in orden]


# ── Export JSON para el dashboard HTML ───────────────────────────────────────
def export_json(conn: sqlite3.Connection):
    rows  = all_games_latest(conn)
    games = group_games(rows)
    data  = {
        "generated_at": datetime.now().isoformat(),
        "total":        len(games),
        "total_raw":    len(rows),
        "games":        games,
    }
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("📄 JSON exportado → %s (%d únicos de %d entradas)", OUTPUT_JSON, len(games), len(rows))


# ── Limpieza de historial antiguo ─────────────────────────────────────────────
def purge_old_history(conn: sqlite3.Connection, days: int = 90):
    """
    Elimina entradas de price_history con más de `days` días de antigüedad,
    EXCEPTO la entrada más reciente de cada juego (para no perder el último precio).
    Mantiene la DB pequeña para que el commit al repo sea manejable.
    """
    cutoff = (datetime.now() - __import__("datetime").timedelta(days=days)).isoformat()
    cur = conn.execute("""
        DELETE FROM price_history
        WHERE scraped_at < ?
          AND id NOT IN (
              SELECT MAX(id) FROM price_history GROUP BY game_id
          )
    """, (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    if deleted:
        # VACUUM para reducir tamaño del archivo
        conn.execute("VACUUM")
        log.info("🧹 Historial limpiado: %d entradas eliminadas (>%d días)", deleted, days)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("═" * 60)
    log.info("ScanPrice Board Games — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("═" * 60)

    conn    = init_db()
    session = requests.Session()

    n_mathom  = scrape_mathom(conn, session)
    n_goblin  = scrape_goblintrader(conn, session)
    n_dungeon = scrape_dungeonmarvels(conn, session)

    export_json(conn)
    purge_old_history(conn, days=90)
    conn.close()

    log.info("")
    log.info("🎲 Scraping completado.")
    log.info("   Nuevos en Mathom:         %d", n_mathom)
    log.info("   Nuevos en GoblinTrader:   %d", n_goblin)
    log.info("   Nuevos en DungeonMarvels: %d", n_dungeon)
    log.info("   TOTAL nuevos:             %d", n_mathom + n_goblin + n_dungeon)
    log.info("═" * 60)


if __name__ == "__main__":
    main()

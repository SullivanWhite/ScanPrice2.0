# ScanPrice 2.0

Monitor de precios de juegos de mesa para las tiendas **Mathom**, **GoblinTrader** y **Dungeon Marvels**. Scraping diario automático, historial de precios en base de datos SQLite, detección de juegos nuevos y dashboard web accesible desde cualquier dispositivo.

 **Dashboard en vivo:** [https://sullivanwhite.github.io/ScanPrice2.0](https://sullivanwhite.github.io/ScanPrice2.0)

---

## Índice

- [¿Qué hace?](#qué-hace)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Instalación local](#instalación-local)
- [Uso manual](#uso-manual)
- [Sistema automático diario](#sistema-automático-diario)
- [Añadir o cambiar URLs de Dungeon Marvels](#añadir-o-cambiar-urls-de-dungeon-marvels)
- [Base de datos](#base-de-datos)
- [Dashboard web](#dashboard-web)
- [Límites y consideraciones](#límites-y-consideraciones)

---

## ¿Qué hace?

1. **Scrapea** tres fuentes de juegos de mesa:
   - [Mathom](https://mathom.es/es/244-juegos-de-tablero) — catálogo completo con paginación (~36 páginas)
   - [GoblinTrader Chollos](https://www.goblintrader.es/es/9910121-Chollos-Juegos-de-Mesa) — sección de ofertas
   - [Dungeon Marvels](https://dungeonmarvels.com) — campaña de ofertas activa (URL configurable)

2. **Guarda** cada juego y su precio en una base de datos SQLite (`games.db`), registrando solo cuando el precio cambia para no generar datos redundantes.

3. **Detecta** juegos nuevos que no estaban en el scrape anterior y los marca con una etiqueta **✦ nuevo** en el dashboard.

4. **Exporta** un archivo `games_data.json` que consume el dashboard web.

5. **Agrupa** juegos duplicados entre tiendas por nombre normalizado, mostrando el precio de cada tienda en la misma card.

6. **Limpia** automáticamente el historial de precios con más de 90 días de antigüedad para mantener la base de datos liviana.

---

## Estructura del proyecto

```
ScanPrice2.0/
├── index.html                  # Dashboard web (se abre en el navegador)
├── scraper.py                  # Script de scraping y gestión de la DB
├── requirements.txt            # Dependencias Python
├── games.db                    # Base de datos SQLite (generada automáticamente)
├── games_data.json             # JSON exportado para el dashboard
├── scraper.log                 # Log de cada ejecución
└── .github/
    └── workflows/
        └── scrape.yml          # Workflow de GitHub Actions (ejecución automática)
```

---

## Instalación local

### Requisitos
- Python 3.11 o superior
- pip

### Pasos

```bash
# 1. Clonar el repositorio
git clone https://github.com/SullivanWhite/ScanPrice2.0.git
cd ScanPrice2.0

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Ejecutar el scraper
python scraper.py
```

`requirements.txt` contiene:
```
requests
beautifulsoup4
lxml
```

---

## Uso manual

```bash
python scraper.py
```

El scraper:
- Crea `games.db` si no existe (o la reutiliza si ya existe)
- Scrapea todas las fuentes con una pausa de 1.5s entre peticiones para no sobrecargar los servidores
- Muestra el progreso en consola y lo guarda en `scraper.log`
- Genera `games_data.json` al terminar
- Limpia entradas de historial con más de 90 días

Para ver el dashboard, abre `index.html` en el navegador. Si los archivos están en local necesitas servirlo con un servidor HTTP para que cargue el JSON:

```bash
# Python incluye un servidor HTTP simple
python -m http.server 8000
# Luego abre http://localhost:8000 en el navegador
```

---

## Sistema automático diario

El archivo `.github/workflows/scrape.yml` configura GitHub Actions para ejecutar el scraper automáticamente cada día.

### Horario
```yaml
- cron: '0 7 * * *'   # 7:00 UTC = 9:00 hora española (verano)
```

>  GitHub Actions en cuentas gratuitas puede retrasarse 15-30 minutos según la carga de sus servidores. Es normal.

### Qué hace el workflow cada día

1. Descarga el repositorio (incluyendo la `games.db` persistida)
2. Instala las dependencias Python
3. Ejecuta `python scraper.py`
4. Commitea automáticamente `games.db` y `games_data.json` al repositorio
5. GitHub Pages publica el nuevo JSON y el dashboard se actualiza

### Verificar que se ejecutó

- **Pestaña Actions** del repositorio → "Daily Scrape" → historial de ejecuciones con hora, duración y estado ✅ / ❌
- **Pestaña Code** → commits de `github-actions[bot]` con mensaje `chore: update prices YYYY-MM-DD`

### Si falla

GitHub puede enviar un email de notificación cuando un workflow falla. Activar en: **Settings de tu cuenta GitHub → Notifications → Actions**.

### Lanzar manualmente

En cualquier momento puedes lanzar el scraper desde:
**Actions → Daily Scrape → Run workflow → Run workflow**

---

## Añadir o cambiar URLs de Dungeon Marvels

Dungeon Marvels publica campañas mensuales de ofertas con URLs que cambian cada mes. La configuración está al principio de `scraper.py`:

```python
# ↓ Cambia esta URL cada vez que haya una nueva campaña
DUNGEON_MARVELS_PROMO = "https://dungeonmarvels.com/1968-liquidacion-mayo-juegos"
```

### Cómo encontrar la nueva URL

1. Entra en [dungeonmarvels.com](https://dungeonmarvels.com)
2. Haz clic en el banner de la promoción activa
3. Copia la URL de la barra del navegador
4. Reemplaza el valor de `DUNGEON_MARVELS_PROMO` en `scraper.py`
5. Commitea el cambio al repositorio

Si no hay campaña activa, ponla a `None`:
```python
DUNGEON_MARVELS_PROMO = None
```

> Los juegos de campañas anteriores quedan en la base de datos con su historial de precios intacto.

---

## Base de datos

La base de datos `games.db` (SQLite) se persiste en el repositorio y se actualiza en cada ejecución.

### Tablas

**`games`** — catálogo de juegos (una entrada por URL única)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER | Clave primaria |
| `source` | TEXT | `mathom`, `goblintrader` o `dungeonmarvels` |
| `name` | TEXT | Nombre del juego |
| `url` | TEXT | URL del producto (única) |
| `first_seen` | TEXT | Fecha de primera detección (ISO 8601) |
| `is_new` | INTEGER | `1` si es nuevo desde el último scrape, `0` si ya estaba |

**`price_history`** — historial de precios (solo se escribe cuando el precio cambia)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INTEGER | Clave primaria |
| `game_id` | INTEGER | Referencia a `games.id` |
| `price` | REAL | Precio actual |
| `original_price` | REAL | Precio sin descuento (si lo hay) |
| `discount_pct` | REAL | Porcentaje de descuento |
| `scraped_at` | TEXT | Fecha y hora del registro (ISO 8601) |

### Optimizaciones

- **Sin duplicados de precio:** si el precio no cambia respecto al registro anterior, no se escribe una nueva entrada en `price_history`
- **Limpieza automática:** entradas con más de 90 días se eliminan automáticamente al terminar cada scrape (excepto la más reciente de cada juego)
- **Anti-bucle de paginación:** detecta cuando una tienda devuelve la misma página repetida (comportamiento de PrestaShop al pedir páginas inexistentes) y para el scraping

---

## Dashboard web

El archivo `index.html` es un dashboard de una sola página sin dependencias externas (solo Google Fonts).

### Funcionalidades

- **Búsqueda** en tiempo real por nombre de juego
- **Filtros** por fuente (Mathom / GoblinTrader / Dungeon Marvels) y por estado (Nuevos / Ofertas)
- **Ordenación** por precio ascendente/descendente, mayor descuento, nombre A→Z, o nuevos primero
- **Paginación** de 40 juegos por página con navegación numérica
- **Cards con múltiples precios:** si un juego aparece en más de una tienda, se muestra una fila por tienda dentro de la misma card, con enlace directo a cada producto
- **Badge ✦ nuevo** en juegos detectados por primera vez en el último scrape
- **Badge de descuento** con porcentaje y precio original tachado

### Colores por tienda

| Tienda | Color |
|---|---|
| Mathom | 🟢 Verde (`#4ecca3`) |
| GoblinTrader | 🟡 Amarillo (`#f0c040`) |
| Dungeon Marvels | 🟠 Naranja (`#f07040`) |

---

## Límites y consideraciones

### GitHub Actions (cuenta gratuita)
- **2.000 minutos/mes** de ejecución gratuita
- El scraper tarda ~4-5 minutos al día → ~120-150 minutos/mes → muy por debajo del límite

### Almacenamiento del repositorio
- GitHub permite hasta **1 GB** por repositorio en cuentas gratuitas
- `games.db` crece ~1-2 MB/mes con la limpieza de 90 días activa
- `games_data.json` ~2-3 MB

### Respeto a las tiendas
- El scraper incluye una pausa de **1.5 segundos entre peticiones** para no sobrecargar los servidores
- Solo lee páginas públicas de catálogo, no realiza ninguna acción de compra ni accede a datos privados

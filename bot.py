import os
import math
import sqlite3
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_RESULTS = 2
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'plazas.db')


# ─── Utilidades ────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ─── Fuentes de datos ──────────────────────────────────────────────────────

def query_overpass(lat: float, lon: float, radius: int) -> list:
    query = f"""
    [out:json][timeout:10];
    (
      node["amenity"="parking"]["capacity:disabled"](around:{radius},{lat},{lon});
      node["parking"="disabled"](around:{radius},{lat},{lon});
      node["amenity"="parking_space"]["access"="disabled"](around:{radius},{lat},{lon});
      way["amenity"="parking"]["capacity:disabled"](around:{radius},{lat},{lon});
    );
    out center body;
    """
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=15)
        r.raise_for_status()
        results = []
        for e in r.json().get("elements", []):
            elat = e.get("center", {}).get("lat") or e.get("lat")
            elon = e.get("center", {}).get("lon") or e.get("lon")
            if elat and elon:
                tags = e.get("tags", {})
                results.append({
                    'lat': elat, 'lon': elon, 'tags': tags,
                    'fuente': 'OpenStreetMap',
                    '_dist': haversine(lat, lon, elat, elon)
                })
        return results
    except Exception as e:
        logger.error(f"Error Overpass: {e}")
        return []


def query_local_db(lat: float, lon: float, radius: int) -> list:
    if not os.path.exists(DB_PATH):
        return []
    lat_d = radius / 111000
    lon_d = radius / (111000 * abs(math.cos(math.radians(lat))) + 1e-9)
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            'SELECT ciudad, lat, lon, fuente FROM plazas '
            'WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?',
            (lat - lat_d, lat + lat_d, lon - lon_d, lon + lon_d)
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Error SQLite: {e}")
        return []

    results = []
    for ciudad, plat, plon, fuente in rows:
        dist = haversine(lat, lon, plat, plon)
        if dist <= radius:
            results.append({
                'lat': plat, 'lon': plon,
                'tags': {'name': f'Plaza – {ciudad}'},
                'fuente': fuente,
                '_dist': dist
            })
    return results


def merge_results(osm: list, local: list) -> list:
    """Combina OSM + local eliminando duplicados por proximidad (<15m)."""
    combined = list(osm)
    for loc in local:
        if not any(haversine(loc['lat'], loc['lon'], o['lat'], o['lon']) < 15 for o in osm):
            combined.append(loc)
    combined.sort(key=lambda x: x['_dist'])
    return combined[:MAX_RESULTS]


def search_plazas(lat: float, lon: float):
    """Busca en OSM + DB. Amplía a 2km si no hay resultados a 500m."""
    for radius in (500, 2000):
        osm = query_overpass(lat, lon, radius)
        local = query_local_db(lat, lon, radius)
        combined = merge_results(osm, local)
        if combined:
            return combined, radius
    return [], 2000


# ─── Formato ───────────────────────────────────────────────────────────────

def format_result(plaza: dict, idx: int) -> str:
    tags = plaza.get('tags', {})
    dist = int(plaza['_dist'])
    fuente = plaza.get('fuente', 'OpenStreetMap')
    nombre = tags.get('name', f'Plaza #{idx}')
    plazas_n = tags.get('capacity:disabled', '')
    direccion = tags.get('addr:street', '')
    if tags.get('addr:housenumber'):
        direccion += f" {tags['addr:housenumber']}"
    lineas = [f"📍 *Plaza {idx}*"]
    if direccion:
        lineas.append(f"🏠 {direccion}")
    if plazas_n:
        lineas.append(f"♿ Plazas reservadas: {plazas_n}")
    lineas.append(f"➡️ A {dist} m · {fuente}")
    return "\n".join(lineas)


# ─── Handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "♿ *Bot de Aparcamiento para Discapacitados*\n\n"
        "Envíame tu 📍 *ubicación* y te mostraré las plazas más cercanas.\n\n"
        "Usa /ayuda para más información.",
        parse_mode="Markdown"
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ciudades = ""
    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute('SELECT ciudad, COUNT(*) FROM plazas GROUP BY ciudad ORDER BY ciudad').fetchall()
        conn.close()
        ciudades = "\n".join(f"  • {c} ({n} plazas)" for c, n in rows)

    await update.message.reply_text(
        "ℹ️ *Cómo usar el bot:*\n\n"
        "1️⃣ Pulsa el clip 📎 → *Ubicación*\n"
        "2️⃣ Busca en 500 m, amplía a 2 km si no hay resultados\n"
        "3️⃣ Navega a cada plaza con Google Maps\n\n"
        "📊 *Fuentes de datos:*\n"
        "• OpenStreetMap\n"
        "• Datos oficiales de ayuntamientos en datos.gob.es\n"
        "/start — Inicio · /ayuda — Esta ayuda · /nuevaplaza — Envía a OpenStreetMap una nueva plaza",
        parse_mode="Markdown"
    )

async def nueva_plaza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗺️ *Añadir una plaza nueva*\n\n"
        "Envíame tu 📍 *ubicación* cerca de la plaza y te daré el enlace para añadirla a OpenStreetMap.",
        parse_mode="Markdown"
    )
    context.user_data['esperando_nueva_plaza'] = True


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_lat = update.message.location.latitude
    user_lon = update.message.location.longitude

    if context.user_data.get('esperando_nueva_plaza'):
        context.user_data['esperando_nueva_plaza'] = False
        url = f"https://www.openstreetmap.org/edit?lat={user_lat}&lon={user_lon}#map=19/{user_lat}/{user_lon}"
        await update.message.reply_text(
            "✅ Aquí tienes el enlace para añadir la plaza en OpenStreetMap:\n\n"
            f"[Abrir editor OSM]({url})\n\n"
            "Añade un nodo con la etiqueta `amenity=parking_space` y `access=disabled`.",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return
    
    msg = await update.message.reply_text("🔍 Buscando plazas cercanas...")

    plazas, radio = search_plazas(user_lat, user_lon)

    if not plazas:
        await msg.edit_text(
            "😔 No encontré plazas en un radio de 2 km.\n\n"
            "Puede que no estén mapeadas aún. Puedes contribuir en openstreetmap.org"
        )
        return

    radio_txt = "500 m" if radio == 500 else "2 km"
    top2 = plazas[:2]

    texto = f"♿ *{len(plazas)} plaza(s) encontrada(s) en {radio_txt}*\n\n"
    texto += "\n\n".join(format_result(p, i+1) for i, p in enumerate(top2))

    keyboard = []
    for i, plaza in enumerate(top2, 1):
        lat, lon = plaza['lat'], plaza['lon']
        keyboard.append([
            InlineKeyboardButton(f"🧭 Cómo llegar a Plaza {i}", url=f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=driving"),
        ])

    await msg.edit_text(
        texto,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Envíame tu *ubicación* para buscar plazas cercanas.\n"
        "Pulsa el clip 📎 → Ubicación.",
        parse_mode="Markdown"
    )


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("Falta la variable de entorno TELEGRAM_TOKEN")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("nuevaplaza", nueva_plaza))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot iniciado...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

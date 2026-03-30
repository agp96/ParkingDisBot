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
    CallbackQueryHandler, 
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_RESULTS = 2
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'plazas.db')

TEXTS = {
    'es': {
        'searching': "🔍 Buscando plazas cercanas...",
        'parking_spaces': "♿ Plazas reservadas: {plazas_n}",
        'distance': "➡️ A {dist} m · {fuente}",
        'not_found': "😔 No encontré plazas en un radio de 2 km.\n\nPuede que no estén mapeadas aún. Añade una nueva plaza con /newparking",
        'found': "♿ *{n} plaza(s) encontrada(s) en {radio}*",
        'directions': "🧭 Cómo llegar a Plaza {i}",
        'send_location': "📍 Envíame tu *ubicación* para buscar plazas cercanas.\nPulsa el clip 📎 → Ubicación.",
        'help_1': "1️⃣ Pulsa el clip 📎 → *Ubicación*",
        'help_2': "2️⃣ Busca en 500 m, amplía a 2 km si no hay resultados",
        'help_3': "3️⃣ Navega a cada plaza con Google Maps",
        'nueva_plaza_prompt': "📍 Envía la ubicación exacta de la plaza y la añadiremos.",
        'nueva_plaza_ok': "✅ Plaza enviada. ¡Gracias!",
    },
    'en': {
        'searching': "🔍 Searching for nearby spaces...",
        'parking_spaces': "♿ Parking spaces: {plazas_n}",
        'distance': "➡️ {dist} m away · {fuente}",
        'not_found': "😔 No disabled parking spaces found within 2 km.\n\nThey may not be mapped yet. Send a new parking space with /newparking",
        'found': "♿ *{n} park(s) found within {radio}*",
        'directions': "🧭 Directions to Parking {i}",
        'send_location': "📍 Send me your *location* to find nearby spaces.\nTap the clip 📎 → Location.",
        'help_1': "1️⃣ Tap the clip 📎 → *Location*",
        'help_2': "2️⃣ Searches within 500 m, expands to 2 km if nothing found",
        'help_3': "3️⃣ Navigate to each space with Google Maps",
        'nueva_plaza_prompt': "📍 Send the exact location of the parking space and we'll add it.",
        'nueva_plaza_ok': "✅ Parking space submitted. Thank you!",
    }
}

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
            'WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND estado="verificada"',
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

def format_result(plaza: dict, idx: int, lang: str) -> str:
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
        lineas.append(TEXTS[lang]['parking_spaces'].format(plazas_n=plazas_n))
    
    lineas.append(TEXTS[lang]['distance'].format(dist=dist, fuente=fuente))    

    return "\n".join(lineas)


# ─── Handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    ]]
    await update.message.reply_text(
        "♿ *ParkingDisBot*\n\n"
        "🇪🇸 Envíame tu 📍 ubicación para encontrar plazas PMR cercanas.\n"
        "🇬🇧 Send me your 📍 location to find nearby disabled parking spaces.\n\n"
        "🌍 Select language / Selecciona idioma:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = query.data.split("_")[1]
    context.user_data['lang'] = lang
    if lang == 'en':
        await query.edit_message_text("🇬🇧 Language set to English. Send me your 📍 location!")
    else:
        await query.edit_message_text("🇪🇸 Idioma establecido en español. ¡Envíame tu 📍 ubicación!")
        
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ciudades = ""
    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute('SELECT ciudad, COUNT(*) FROM plazas GROUP BY ciudad ORDER BY ciudad').fetchall()
        conn.close()
        ciudades = "\n".join(f"  • {c} ({n} plazas)" for c, n in rows)

    lang = context.user_data.get('lang', 'es')
    texto = f"{TEXTS[lang]['help_1']}\n{TEXTS[lang]['help_2']}\n{TEXTS[lang]['help_3']}"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def nueva_plaza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get('lang', 'es')
    await update.message.reply_text(
        TEXTS[lang]['nueva_plaza_prompt'],
        parse_mode="Markdown"
    )
    context.user_data['esperando_nueva_plaza'] = True


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_lat = update.message.location.latitude
    user_lon = update.message.location.longitude
    
    lang = context.user_data.get('lang', 'es')

    if context.user_data.get('esperando_nueva_plaza'):
        context.user_data['esperando_nueva_plaza'] = False
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            'INSERT INTO plazas (ciudad, lat, lon, fuente, estado) VALUES (?, ?, ?, ?, ?)',
            ('Desconocida', user_lat, user_lon, 'Usuario', 'pendiente')
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(TEXTS[lang]['nueva_plaza_ok'])
        return
    
    msg = await update.message.reply_text(TEXTS[lang]['searching'])

    plazas, radio = search_plazas(user_lat, user_lon)

    if not plazas:
        await msg.edit_text(
            TEXTS[lang]['not_found']
        )
        return

    radio_txt = "500 m" if radio == 500 else "2 km"
    top2 = plazas[:2]
    
    texto = TEXTS[lang]['found'].format(n=len(plazas), radio=radio_txt) + "\n\n"
    texto += "\n\n".join(format_result(p, i+1, lang) for i, p in enumerate(top2))

    keyboard = []
    for i, plaza in enumerate(top2, 1):
        lat, lon = plaza['lat'], plaza['lon']
        keyboard.append([
            InlineKeyboardButton(TEXTS[lang]['directions'].format(i=i), url=f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=driving"),
        ])

    await msg.edit_text(
        texto,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get('lang', 'es')
    await update.message.reply_text(
        TEXTS[lang]['send_location'],
        parse_mode="Markdown"
    )


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("Falta la variable de entorno TELEGRAM_TOKEN")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(set_language, pattern="^lang_"))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("nuevaplaza", nueva_plaza))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot iniciado...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

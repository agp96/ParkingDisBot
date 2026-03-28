import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SEARCH_RADIUS = 800  # metros
MAX_RESULTS = 8


def query_overpass(lat: float, lon: float, radius: int = SEARCH_RADIUS) -> list:
    """Consulta Overpass API para plazas de discapacitados cercanas."""
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
        response = requests.post(OVERPASS_URL, data={"data": query}, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data.get("elements", [])
    except Exception as e:
        logger.error(f"Error Overpass API: {e}")
        return []


def haversine(lat1, lon1, lat2, lon2) -> float:
    """Calcula distancia en metros entre dos coordenadas."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def format_result(element: dict, user_lat: float, user_lon: float, idx: int) -> tuple:
    """Formatea un resultado de OSM y devuelve (texto, lat, lon)."""
    if element["type"] == "way":
        lat = element.get("center", {}).get("lat")
        lon = element.get("center", {}).get("lon")
    else:
        lat = element.get("lat")
        lon = element.get("lon")

    if not lat or not lon:
        return None, None, None

    tags = element.get("tags", {})
    distancia = int(haversine(user_lat, user_lon, lat, lon))

    nombre = tags.get("name", tags.get("operator", f"Plaza {idx}"))
    plazas = tags.get("capacity:disabled", tags.get("capacity", "?"))
    direccion = tags.get("addr:street", "")
    if tags.get("addr:housenumber"):
        direccion += f" {tags['addr:housenumber']}"

    lineas = [f"📍 *{nombre}*"]
    if direccion:
        lineas.append(f"🏠 {direccion}")
    if plazas != "?":
        lineas.append(f"♿ Plazas reservadas: {plazas}")
    lineas.append(f"📏 A {distancia} m de tu ubicación")

    return "\n".join(lineas), lat, lon


# ─── Handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "♿ *Bot de Aparcamiento para Discapacitados*\n\n"
        "Envíame tu 📍 *ubicación* y te mostraré las plazas reservadas más cercanas según OpenStreetMap.\n\n"
        "También puedes usar /ayuda para más información.",
        parse_mode="Markdown"
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Cómo usar el bot:*\n\n"
        "1️⃣ Pulsa el clip 📎 → *Ubicación* y envía tu posición\n"
        "2️⃣ El bot buscará plazas PMR en un radio de ~800 m\n"
        "3️⃣ Podrás ver cada plaza en Google Maps\n\n"
        "⚠️ Los datos provienen de *OpenStreetMap*. "
        "Si falta alguna plaza, puedes contribuir en openstreetmap.org\n\n"
        "Comandos:\n"
        "/start — Inicio\n"
        "/ayuda — Esta ayuda",
        parse_mode="Markdown"
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_lat = update.message.location.latitude
    user_lon = update.message.location.longitude

    msg = await update.message.reply_text("🔍 Buscando plazas cercanas...")

    elements = query_overpass(user_lat, user_lon, radius=800)

    if not elements:
        await msg.edit_text("🔍 Nada en 800 m, ampliando a 2 km...")
        elements = query_overpass(user_lat, user_lon, radius=2000)

    if not elements:
        await msg.edit_text(
            "😔 No encontré plazas de aparcamiento para discapacitados en un radio de 2 km.\n\n"
            "Puede que no estén mapeadas en OpenStreetMap. "
            "Puedes contribuir en openstreetmap.org"
        )
        return

    # Ordenar por distancia
    def distancia_elem(e):
        lat = e.get("center", {}).get("lat") or e.get("lat")
        lon = e.get("center", {}).get("lon") or e.get("lon")
        if lat and lon:
            return haversine(user_lat, user_lon, lat, lon)
        return float("inf")

    elements.sort(key=distancia_elem)
    elements = elements[:MAX_RESULTS]

    resultados_validos = 0
    for idx, element in enumerate(elements, 1):
        texto, lat, lon = format_result(element, user_lat, user_lon, idx)
        if not texto:
            continue

        keyboard = [[
            InlineKeyboardButton(
                "🗺️ Ver en Google Maps",
                url=f"https://www.google.com/maps?q={lat},{lon}"
            ),
            InlineKeyboardButton(
                "🧭 Cómo llegar",
                url=f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=driving"
            )
        ]]

        await update.message.reply_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        resultados_validos += 1

    await msg.edit_text(
        f"✅ Encontré *{resultados_validos}* plaza(s) en un radio de 800 m.",
        parse_mode="Markdown"
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
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot iniciado...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

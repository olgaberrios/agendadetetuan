#!/usr/bin/env python3
"""
Bot Agenda Tetuán
────────────────────────────────────────────────────────────
Lee mensajes (texto e imágenes) del canal @agendatetuan,
extrae eventos usando la IA de Claude, y los sube a GitHub Pages.

Requisitos:
  pip install python-telegram-bot anthropic PyGithub python-dotenv

Configura el archivo .env antes de arrancar.
"""

import os
import json
import base64
import hashlib
import logging
import re
from datetime import datetime, timezone
from io import BytesIO

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, filters, ContextTypes
)
import anthropic
from github import Github, GithubException

load_dotenv()
load_dotenv()
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
log = logging.getLogger(__name__)

# --- CONFIG ---
log.info("Cargando configuracion...")

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "")
CHANNEL_USERNAME  = os.environ.get("CHANNEL_USERNAME", "@agendatetuan")
EVENTS_JSON_PATH  = "events.json"
REVIEW_CHAT_ID    = os.environ.get("REVIEW_CHAT_ID")

log.info(f"  TELEGRAM_TOKEN:    {'OK' if TELEGRAM_TOKEN else 'FALTA'}")
log.info(f"  ANTHROPIC_API_KEY: {'OK' if ANTHROPIC_API_KEY else 'FALTA'}")
log.info(f"  GITHUB_TOKEN:      {'OK' if GITHUB_TOKEN else 'FALTA'}")
log.info(f"  GITHUB_REPO:       {GITHUB_REPO if GITHUB_REPO else 'FALTA'}")

if not TELEGRAM_TOKEN:
    log.error("TELEGRAM_TOKEN no encontrado.")
    import sys; sys.exit(1)

# --- CLIENTES ---
log.info("Conectando con Anthropic y GitHub...")
try:
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    log.info("  Anthropic: OK")
except Exception as e:
    log.error(f"  Anthropic error: {e}")
    import sys; sys.exit(1)

try:
    gh   = Github(GITHUB_TOKEN)
    repo = gh.get_repo(GITHUB_REPO)
    log.info(f"  GitHub repo OK: {GITHUB_REPO}")
except Exception as e:
    log.error(f"  GitHub error: {e}")
    import sys; sys.exit(1)

# ─── PROMPT PARA CLAUDE ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """
Eres un asistente que extrae información de eventos culturales y vecinales del
barrio de Tetuán (Madrid) a partir de mensajes de Telegram (texto, carteles...).

Devuelve SOLO un objeto JSON con esta estructura (sin ningún texto adicional,
sin comillas de bloque de código):

{
  "es_evento": true o false,
  "title": "Título del evento",
  "datetime": "YYYY-MM-DDTHH:MM:SS",
  "end_datetime": "YYYY-MM-DDTHH:MM:SS o null",
  "location": "Lugar del evento o null",
  "description": "Descripción completa del evento"
}

Reglas:
- Si el mensaje NO anuncia un evento concreto (p.ej. es spam, noticias sin fecha,
  saludos, conversación), devuelve {"es_evento": false}.
- El campo datetime es OBLIGATORIO si es_evento es true. Si no hay hora, usa 00:00:00.
- El año actual es """ + str(datetime.now().year) + """.
- Si la fecha dice "este viernes", "mañana", etc., calcúlala respecto a hoy: """ + datetime.now().strftime("%Y-%m-%d") + """.
- location: intenta extraer la dirección o el nombre del lugar. Si no hay, pon null.
- description: incluye toda la información útil del cartel/mensaje que no esté
  en los otros campos (precio, organización, contacto, descripción, etc.).
- Responde ÚNICAMENTE con el JSON. Nada más.
"""

# ─── EXTRAER EVENTO ───────────────────────────────────────────────────────────
def extract_event_from_text(text: str) -> dict | None:
    """Pasa texto a Claude y devuelve el evento extraído o None."""
    try:
        resp = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}]
        )
        raw = resp.content[0].text.strip()
        data = json.loads(raw)
        return data if data.get("es_evento") else None
    except Exception as e:
        log.error(f"Error extrayendo evento de texto: {e}")
        return None


def extract_event_from_image(image_bytes: bytes, caption: str = "") -> dict | None:
    """Pasa una imagen (cartel) a Claude Vision y devuelve el evento extraído o None."""
    try:
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        content = []
        if caption:
            content.append({"type": "text", "text": f"Texto que acompaña la imagen: {caption}\n\nAhora analiza el cartel:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })
        content.append({"type": "text", "text": "Extrae el evento de este cartel siguiendo las instrucciones del sistema."})

        resp = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}]
        )
        raw = resp.content[0].text.strip()
        data = json.loads(raw)
        return data if data.get("es_evento") else None
    except Exception as e:
        log.error(f"Error extrayendo evento de imagen: {e}")
        return None

# ─── GITHUB: LEER Y ESCRIBIR events.json ─────────────────────────────────────
def load_events_from_github() -> tuple[list, str]:
    """Devuelve (lista_eventos, sha_del_fichero)."""
    try:
        f = repo.get_contents(EVENTS_JSON_PATH)
        events = json.loads(f.decoded_content.decode("utf-8"))
        return events, f.sha
    except GithubException:
        return [], ""


def save_events_to_github(events: list, sha: str) -> bool:
    """Guarda la lista de eventos en GitHub. Devuelve True si tuvo éxito."""
    content = json.dumps(events, ensure_ascii=False, indent=2)
    try:
        if sha:
            repo.update_file(
                EVENTS_JSON_PATH,
                "🗓️ Evento añadido automáticamente por el bot",
                content,
                sha
            )
        else:
            repo.create_file(
                EVENTS_JSON_PATH,
                "🗓️ Crear events.json",
                content
            )
        return True
    except GithubException as e:
        log.error(f"Error guardando en GitHub: {e}")
        return False

# ─── AÑADIR EVENTO AL JSON ────────────────────────────────────────────────────
def add_event(event_data: dict, source_id: str) -> bool:
    """Añade el evento al JSON de GitHub evitando duplicados."""
    events, sha = load_events_from_github()

    # Deduplicar por source_id (id del mensaje de Telegram)
    if any(e.get("source_id") == source_id for e in events):
        log.info(f"Evento duplicado, ignorando: {source_id}")
        return False

    # Generar id único
    event_id = hashlib.md5(f"{source_id}{event_data.get('datetime','')}".encode()).hexdigest()[:10]

    new_event = {
        "id": event_id,
        "title":        event_data.get("title", "Sin título"),
        "datetime":     event_data.get("datetime", ""),
        "end_datetime": event_data.get("end_datetime"),
        "location":     event_data.get("location"),
        "description":  event_data.get("description", ""),
        "source_id":    source_id,
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }

    events.append(new_event)

    # Ordenar por fecha
    events.sort(key=lambda e: e.get("datetime", ""))

    return save_events_to_github(events, sha)

# ─── NOTIFICACIÓN OPCIONAL AL ADMIN ──────────────────────────────────────────
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, event_data: dict, ok: bool):
    """Manda un mensaje privado al admin confirmando o avisando de error."""
    if not REVIEW_CHAT_ID:
        return
    if ok:
        text = (
            f"✅ *Evento añadido a la web*\n\n"
            f"*{event_data.get('title')}*\n"
            f"📅 {event_data.get('datetime','')}\n"
            f"📍 {event_data.get('location') or 'Sin ubicación'}"
        )
    else:
        text = (
            f"⚠️ *No se pudo guardar un evento en GitHub*\n\n"
            f"*{event_data.get('title')}*\n"
            f"Revisa los logs del bot."
        )
    await context.bot.send_message(
        chat_id=REVIEW_CHAT_ID,
        text=text,
        parse_mode="Markdown"
    )

# ─── HANDLER: MENSAJES DE TEXTO ───────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or not msg.text:
        return

    log.info(f"📨 Texto recibido (id={msg.message_id})")
    event_data = extract_event_from_text(msg.text)

    if not event_data:
        log.info("   → No es un evento, ignorando")
        return

    log.info(f"   → Evento detectado: {event_data.get('title')}")
    ok = add_event(event_data, source_id=str(msg.message_id))
    await notify_admin(context, event_data, ok)

# ─── HANDLER: FOTOS / CARTELES ────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or not msg.photo:
        return

    log.info(f"🖼️ Foto recibida (id={msg.message_id})")

    # Descargamos la foto en máxima resolución sin enviar nada al canal
    photo = msg.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    image_bytes = buf.getvalue()

    caption = msg.caption or ""
    event_data = extract_event_from_image(image_bytes, caption)

    if not event_data:
        log.info("   → La imagen no contiene un evento reconocible, ignorando")
        return

    log.info(f"   → Evento detectado en imagen: {event_data.get('title')}")
    ok = add_event(event_data, source_id=f"img_{msg.message_id}")
    await notify_admin(context, event_data, ok)

# ─── ARRANQUE ─────────────────────────────────────────────────────────────────
async def main():
    log.info("🚇 Bot Agenda Tetuán arrancando...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info(f"✅ Escuchando mensajes de {CHANNEL_USERNAME}")
    await app.run_polling(allowed_updates=["channel_post", "message"])


if __name__ == "__main__":
    import asyncio
    import sys
    try:
        asyncio.run(main())
    except Exception as e:
        log.error(f"❌ Error fatal: {e}", exc_info=True)
        sys.exit(1)

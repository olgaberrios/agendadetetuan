#!/usr/bin/env python3
"""
Bot Agenda Tetuán — usa OpenRouter (gratis) para leer texto e imágenes
"""

import os
import json
import base64
import hashlib
import logging
import threading
import requests as http_requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from io import BytesIO

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from github import Github, GithubException

load_dotenv()
logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
log.info("Cargando configuracion...")

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "")
CHANNEL_USERNAME  = os.environ.get("CHANNEL_USERNAME", "@agendatetuan")
EVENTS_JSON_PATH  = "events.json"
REVIEW_CHAT_ID    = os.environ.get("REVIEW_CHAT_ID")

log.info(f"  TELEGRAM_TOKEN:      {'OK' if TELEGRAM_TOKEN else 'FALTA'}")
log.info(f"  OPENROUTER_API_KEY:  {'OK' if OPENROUTER_API_KEY else 'FALTA'}")
log.info(f"  GITHUB_TOKEN:        {'OK' if GITHUB_TOKEN else 'FALTA'}")
log.info(f"  GITHUB_REPO:         {GITHUB_REPO if GITHUB_REPO else 'FALTA'}")

if not TELEGRAM_TOKEN:
    log.error("TELEGRAM_TOKEN no encontrado.")
    import sys; sys.exit(1)

# ─── GITHUB ───────────────────────────────────────────────────────────────────
log.info("Conectando con GitHub...")
try:
    gh   = Github(GITHUB_TOKEN)
    repo = gh.get_repo(GITHUB_REPO)
    log.info(f"  GitHub repo OK: {GITHUB_REPO}")
except Exception as e:
    log.error(f"  GitHub error: {e}")
    import sys; sys.exit(1)

# ─── PROMPT ───────────────────────────────────────────────────────────────────
PROMPT = f"""Eres un asistente que extrae información de eventos culturales y vecinales del
barrio de Tetuán (Madrid) a partir de mensajes de Telegram (texto o carteles).

Devuelve SOLO un objeto JSON con esta estructura (sin texto adicional ni bloques de código):

{{
  "es_evento": true o false,
  "title": "Título del evento",
  "datetime": "YYYY-MM-DDTHH:MM:SS",
  "end_datetime": "YYYY-MM-DDTHH:MM:SS o null",
  "location": "Lugar del evento o null",
  "description": "Descripción completa"
}}

Reglas:
- Si el mensaje NO anuncia un evento concreto, devuelve {{"es_evento": false}}.
- datetime es OBLIGATORIO si es_evento es true. Sin hora usa 00:00:00.
- El año actual es {datetime.now().year}. Hoy es {datetime.now().strftime("%Y-%m-%d")}.
- Calcula fechas relativas como "este viernes" o "mañana" respecto a hoy.
- Responde ÚNICAMENTE con el JSON."""

# ─── LLAMADA A OPENROUTER ─────────────────────────────────────────────────────
# Modelos gratuitos con soporte de imágenes, en orden de preferencia
FREE_VISION_MODELS = [
    "qwen/qwen2.5-vl-72b-instruct:free",
    "qwen/qwen2-vl-7b-instruct:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "openrouter/free",
]

FREE_TEXT_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-r1:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "openrouter/free",
]

def call_openrouter(messages: list, vision: bool = False) -> str | None:
    models = FREE_VISION_MODELS if vision else FREE_TEXT_MODELS
    for model in models:
        try:
            log.info(f"  Probando modelo: {model}")
            resp = http_requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://agendatetuan.github.io",
                    "X-Title": "Agenda Tetuán Bot"
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 800
                },
                timeout=30
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"]
            if result:
                log.info(f"  Modelo OK: {model}")
                return result.strip()
        except Exception as e:
            log.warning(f"  Modelo {model} falló: {e}, probando siguiente...")
    log.error("Todos los modelos fallaron")
    return None

# ─── EXTRAER EVENTO ───────────────────────────────────────────────────────────
def extract_from_text(text: str) -> dict | None:
    try:
        raw = call_openrouter([
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text}
        ])
        if not raw:
            return None
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        return data if data.get("es_evento") else None
    except Exception as e:
        log.error(f"Error extrayendo texto: {e}")
        return None

def extract_from_image(image_bytes: bytes, caption: str = "") -> dict | None:
    try:
        b64  = base64.standard_b64encode(image_bytes).decode("utf-8")
        text = PROMPT
        if caption:
            text += f"\n\nTexto que acompaña la imagen: {caption}"
        text += "\n\nAnaliza el cartel y extrae el evento."

        raw = call_openrouter([
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}
        ])
        if not raw:
            return None
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        return data if data.get("es_evento") else None
    except Exception as e:
        log.error(f"Error extrayendo imagen: {e}")
        return None

# ─── GITHUB: LEER Y ESCRIBIR events.json ─────────────────────────────────────
def load_events() -> tuple[list, str]:
    try:
        f = repo.get_contents(EVENTS_JSON_PATH)
        return json.loads(f.decoded_content.decode("utf-8")), f.sha
    except GithubException:
        return [], ""

def save_events(events: list, sha: str) -> bool:
    content = json.dumps(events, ensure_ascii=False, indent=2)
    try:
        if sha:
            repo.update_file(EVENTS_JSON_PATH, "🗓️ Evento añadido por el bot", content, sha)
        else:
            repo.create_file(EVENTS_JSON_PATH, "🗓️ Crear events.json", content)
        return True
    except GithubException as e:
        log.error(f"Error GitHub: {e}")
        return False

def upload_image_to_github(image_bytes: bytes, filename: str) -> str | None:
    """Sube imagen a /images/ en GitHub y devuelve la URL pública."""
    try:
        path = f"images/{filename}"
        try:
            existing = repo.get_contents(path)
            repo.update_file(path, "Actualizar imagen", image_bytes, existing.sha)
        except GithubException:
            repo.create_file(path, "Subir imagen de evento", image_bytes)
        raw_url = f"https://{GITHUB_REPO.split('/')[0]}.github.io/{GITHUB_REPO.split('/')[1]}/{path}"
        log.info(f"  Imagen subida: {raw_url}")
        return raw_url
    except Exception as e:
        log.error(f"  Error subiendo imagen: {e}")
        return None

def cleanup_past_images():
    """Borra de GitHub las imágenes de eventos ya pasados."""
    try:
        events, sha = load_events()
        now = datetime.now(timezone.utc)
        changed = False
        for ev in events:
            if not ev.get("image_url"):
                continue
            dt_str = ev.get("end_datetime") or ev.get("datetime", "")
            if not dt_str:
                continue
            try:
                dt = datetime.fromisoformat(dt_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < now:
                    filename = ev["image_url"].split("/images/")[-1]
                    try:
                        f = repo.get_contents(f"images/{filename}")
                        repo.delete_file(f"images/{filename}", "Borrar imagen de evento pasado", f.sha)
                        log.info(f"  Imagen borrada: {filename}")
                    except GithubException:
                        pass
                    ev["image_url"] = None
                    changed = True
            except Exception:
                continue
        if changed:
            save_events(events, sha)
            log.info("Limpieza de imagenes completada")
    except Exception as e:
        log.error(f"Error en limpieza: {e}")

def image_hash(image_bytes: bytes) -> str:
    """Hash simple de la imagen para detectar carteles repetidos."""
    return hashlib.md5(image_bytes).hexdigest()

def is_duplicate_event(event_data: dict, events: list, img_hash: str = None) -> bool:
    """Detecta si el evento ya existe por título+fecha o por hash de imagen."""
    title = event_data.get("title", "").lower().strip()
    dt    = event_data.get("datetime", "")[:10]  # solo la fecha YYYY-MM-DD
    for ev in events:
        # Duplicado por hash de imagen (mismo cartel reenviado)
        if img_hash and ev.get("image_hash") == img_hash:
            log.info(f"  Cartel duplicado (mismo hash), ignorando")
            return True
        # Duplicado por título similar y misma fecha
        ev_title = ev.get("title", "").lower().strip()
        ev_dt    = ev.get("datetime", "")[:10]
        if dt and ev_dt == dt and (title == ev_title or (len(title) > 10 and title in ev_title) or (len(ev_title) > 10 and ev_title in title)):
            log.info(f"  Evento duplicado (mismo título+fecha), ignorando")
            return True
    return False

def add_event(event_data: dict, source_id: str, image_bytes: bytes = None) -> bool:
    events, sha = load_events()
    if any(e.get("source_id") == source_id for e in events):
        log.info(f"Duplicado por source_id, ignorando: {source_id}")
        return False
    img_hash = image_hash(image_bytes) if image_bytes else None
    if is_duplicate_event(event_data, events, img_hash):
        return False
    event_id = hashlib.md5(f"{source_id}{event_data.get('datetime','')}".encode()).hexdigest()[:10]
    image_url = None
    if image_bytes:
        image_url = upload_image_to_github(image_bytes, f"{event_id}.jpg")
    events.append({
        "id":           event_id,
        "title":        event_data.get("title", "Sin título"),
        "datetime":     event_data.get("datetime", ""),
        "end_datetime": event_data.get("end_datetime"),
        "location":     event_data.get("location"),
        "description":  event_data.get("description", ""),
        "image_url":    image_url,
        "image_hash":   img_hash,
        "source_id":    source_id,
        "created_at":   datetime.now(timezone.utc).isoformat(),
    })
    events.sort(key=lambda e: e.get("datetime", ""))
    return save_events(events, sha)


# ─── NOTIFICACIÓN ADMIN ───────────────────────────────────────────────────────
async def notify_admin(context, event_data: dict, ok: bool):
    if not REVIEW_CHAT_ID:
        return
    if ok:
        text = f"✅ *Evento añadido*\n\n*{event_data.get('title')}*\n📅 {event_data.get('datetime','')}\n📍 {event_data.get('location') or 'Sin ubicación'}"
    else:
        text = f"⚠️ *No se pudo guardar*\n\n*{event_data.get('title')}*"
    await context.bot.send_message(chat_id=REVIEW_CHAT_ID, text=text, parse_mode="Markdown")

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or not msg.text:
        return
    log.info(f"📨 Texto recibido (id={msg.message_id})")
    event_data = extract_from_text(msg.text)
    if not event_data:
        log.info("   → No es un evento, ignorando")
        return
    log.info(f"   → Evento: {event_data.get('title')}")
    ok = add_event(event_data, source_id=str(msg.message_id))
    await notify_admin(context, event_data, ok)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or not msg.photo:
        return
    log.info(f"🖼️ Foto recibida (id={msg.message_id})")
    photo   = msg.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    buf     = BytesIO()
    await tg_file.download_to_memory(buf)
    image_bytes = buf.getvalue()
    event_data = extract_from_image(image_bytes, msg.caption or "")
    if not event_data:
        log.info("   → Sin evento reconocible, ignorando")
        return
    log.info(f"   → Evento en imagen: {event_data.get('title')}")
    ok = add_event(event_data, source_id=f"img_{msg.message_id}", image_bytes=image_bytes)
    await notify_admin(context, event_data, ok)

# ─── SERVIDOR WEB (para que Render no duerma el servicio) ─────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Agenda Tetuan OK")
    def log_message(self, format, *args):
        pass

def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"🌐 Servidor web en puerto {port}")
    server.serve_forever()

# ─── ARRANQUE ─────────────────────────────────────────────────────────────────
async def daily_cleanup(context):
    log.info("⏰ Limpieza diaria de imágenes...")
    cleanup_past_images()

def main():
    import time as time_module
    from datetime import time as dtime

    # Arrancar servidor web primero y esperar a que el puerto esté listo
    t = threading.Thread(target=start_web_server, daemon=True)
    t.start()
    time_module.sleep(2)

    log.info("Bot Agenda Tetuan arrancando...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    if app.job_queue:
        app.job_queue.run_daily(daily_cleanup, time=dtime(3, 0))
        log.info("Limpieza diaria programada a las 3:00 AM")
    else:
        log.warning("JobQueue no disponible")

    log.info(f"Escuchando mensajes de {CHANNEL_USERNAME}")
    app.run_polling(allowed_updates=["channel_post", "message"])

if __name__ == "__main__":
    main()

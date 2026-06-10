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
PROMPT = """Eres un asistente que extrae informacion de eventos culturales y vecinales del
barrio de Tetuan (Madrid) a partir de mensajes de Telegram (texto o carteles).

Devuelve SOLO un array JSON (sin texto adicional ni bloques de codigo).
Normalmente tendra un elemento, pero puede tener varios (ver reglas).

Cada elemento:
{
  "es_evento": true o false,
  "title": "Titulo del evento",
  "datetime": "YYYY-MM-DDTHH:MM:SS",
  "end_datetime": "YYYY-MM-DDTHH:MM:SS o null",
  "location": "Lugar o null",
  "description": "Descripcion completa"
}

Reglas generales:
- Si NO hay ningun evento concreto, devuelve [{"es_evento": false}].
- datetime obligatorio si es_evento es true. Sin hora usa 00:00:00.
- El anno actual es 2026. Hoy es 2026-05-31.
- Calcula fechas relativas como "este viernes" o "manana" respecto a hoy.
- NO inventes informacion. Usa solo lo que aparece en el texto o imagen.
- Copia titulos y descripciones tal como aparecen, sin reescribirlos.

Horarios recurrentes (sin fecha concreta):
- Si el cartel muestra un horario semanal (ej: "Miercoles 18-19h, Sabado 12-14h"),
  crea UN evento por cada ocurrencia, con las 2 proximas fechas de cada dia de la semana.
  Ejemplo: hoy es Sunday 31 de May. Si pone "Miercoles y Sabado", calcula las 2 proximas
  fechas de cada dia y crea 4 eventos en total.
- El titulo de cada evento debe incluir el nombre de la actividad.

Ubicaciones especiales:
- Si es una emisora de radio, pon el nombre en location y frecuencia/web en description.
- Si es una URL, ponla en location tal cual.
- Si no hay direccion fisica, pon el nombre del espacio o medio, no null.

Responde UNICAMENTE con el array JSON."""



# ─── LLAMADA A OPENROUTER ─────────────────────────────────────────────────────
import time as _time

# openrouter/auto elige el mejor modelo para cada petición automáticamente
FREE_MODELS = [
    "openrouter/auto",
    "openrouter/free",
]

def call_openrouter(messages: list, vision: bool = False) -> tuple[str | None, str | None]:
    """Devuelve (resultado, motivo_error). motivo_error es None si todo fue bien."""
    models = FREE_MODELS
    last_error = None
    for model in models:
        for attempt in range(2):
            try:
                log.info(f"  Probando modelo: {model}")
                resp = http_requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://agendatetuan.github.io",
                        "X-Title": "Agenda Tetuan Bot"
                    },
                    json={"model": model, "messages": messages, "max_tokens": 1000},
                    timeout=45
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10))
                    log.warning(f"  429 en {model}, esperando {wait}s...")
                    last_error = "limite_openrouter"
                    _time.sleep(wait)
                    continue
                resp.raise_for_status()
                result = resp.json()["choices"][0]["message"]["content"]
                if result:
                    log.info(f"  Modelo OK: {model}")
                    return result.strip(), None
                break
            except Exception as e:
                log.warning(f"  Modelo {model} intento {attempt+1} falló: {e}")
                if "429" in str(e): last_error = "limite_openrouter"
                elif "401" in str(e): last_error = "clave_invalida"
                else: last_error = "error_modelo"
                if attempt == 0: _time.sleep(3)
    log.error("Todos los modelos fallaron")
    return None, last_error or "error_desconocido"

# ─── EXTRAER EVENTO ───────────────────────────────────────────────────────────
def extract_events(raw: str) -> list[dict]:
    """Parsea la respuesta JSON del modelo y devuelve lista de eventos válidos."""
    if not raw:
        return []
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)
        items = data if isinstance(data, list) else [data]
        return [e for e in items if isinstance(e, dict) and e.get("es_evento")]
    except Exception as e:
        log.error(f"Error parseando JSON: {e} | raw: {raw[:200]}")
        return []

def extract_event_fallback(text: str) -> list[dict]:
    """Extractor sin IA usando regex. Último recurso cuando OpenRouter falla."""
    import re

    MESES_ES = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
                "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}

    fecha = hora = location = None

    # Fecha: "12 de junio", "jueves 12 de junio"
    m = re.search(r"(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d{1,2})\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)", text, re.IGNORECASE)
    if m:
        dia = int(m.group(1))
        mes = MESES_ES[m.group(2).lower()]
        fecha = f"{datetime.now().year}-{mes:02d}-{dia:02d}"

    # Hora: "19:30", "19h30", "a las 19"
    m = re.search(r"(?:a las\s+)?(\d{1,2})[:h\.](\d{2})?(?:\s*h(?:oras?)?)?", text, re.IGNORECASE)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        if 6 <= h <= 23:
            hora = f"{h:02d}:{mi:02d}:00"

    if not fecha:
        return []

    dt = f"{fecha}T{hora if hora else '00:00:00'}"

    # Título: primera línea no vacía
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    title = lines[0] if lines else "Evento sin título"

    # Lugar: "Calle X", "Espacio X", "en X"
    m = re.search(r"[Cc]alle\s+[\w\s]+(?:,\s*\d+)?", text)
    if m:
        location = m.group(0).strip()
    if not location:
        m = re.search(r"(?:espacio|centro|biblioteca|sala|teatro|plaza)\s+[\w\s]+", text, re.IGNORECASE)
        if m:
            location = m.group(0).strip()

    log.info(f"  Fallback regex: título={title}, fecha={dt}, lugar={location}")
    return [{"es_evento": True, "title": title, "datetime": dt, "end_datetime": None,
             "location": location, "description": text}]

def extract_from_text(text: str) -> tuple[list[dict], str | None]:
    try:
        raw, error = call_openrouter([
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text}
        ])
        events = extract_events(raw) if raw else []
        if not events and error:
            log.info(f"  IA falló ({error}), intentando extractor regex...")
            events = extract_event_fallback(text)
            if events:
                log.info("  Fallback regex tuvo éxito")
        return events, error
    except Exception as e:
        log.error(f"Error extrayendo texto: {e}")
        return [], "error_desconocido"

def extract_from_image(image_bytes: bytes, caption: str = "") -> tuple[list[dict], str | None]:
    try:
        b64  = base64.standard_b64encode(image_bytes).decode("utf-8")
        text = PROMPT
        if caption:
            text += f"\n\nTexto que acompaña la imagen: {caption}"
        text += "\n\nAnaliza el cartel y extrae el/los evento/s."

        raw, error = call_openrouter([
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}
        ])
        events = extract_events(raw) if raw else []
        return events, error
    except Exception as e:
        log.error(f"Error extrayendo imagen: {e}")
        return [], "error_desconocido"

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
        raw_url = f"https://agendadetetuan.github.io/calendario/{path}"
        log.info(f"  Imagen subida: {raw_url}")
        return raw_url
    except Exception as e:
        log.error(f"  Error subiendo imagen: {e}")
        return None

def cleanup_past_events():
    """Borra eventos pasados y sus imágenes de GitHub."""
    try:
        events, sha = load_events()
        now = datetime.now(timezone.utc)
        to_keep = []
        changed = False

        for ev in events:
            dt_str = ev.get("end_datetime") or ev.get("datetime", "")
            is_past = False
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < now:
                        is_past = True
                except Exception:
                    pass

            if is_past:
                changed = True
                log.info(f"  Borrando evento pasado: {ev.get('title')}")
                # Borrar imagen si existe
                if ev.get("image_url"):
                    try:
                        filename = ev["image_url"].split("/images/")[-1]
                        f = repo.get_contents(f"images/{filename}")
                        repo.delete_file(f"images/{filename}", "Borrar imagen de evento pasado", f.sha)
                        log.info(f"  Imagen borrada: {filename}")
                    except GithubException:
                        pass
            else:
                to_keep.append(ev)

        if changed:
            save_events(to_keep, sha)
            log.info(f"Limpieza completada: {len(events)-len(to_keep)} eventos borrados, {len(to_keep)} conservados")
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
ERROR_MSGS = {
    "limite_openrouter": "⚠️ Límite de peticiones a OpenRouter agotado. Inténtalo en unos minutos.",
    "clave_invalida": "⚠️ Clave de OpenRouter inválida. Revisa la configuración.",
    "error_modelo": "⚠️ El modelo de IA no respondió correctamente.",
    "error_desconocido": "⚠️ Error desconocido al procesar."
}

def build_ignore_reason(event_data: dict | None, error: str | None) -> str:
    """Construye un mensaje de error descriptivo."""
    if error and error == "limite_openrouter":
        return "⚠️ *Peticiones a OpenRouter agotadas*. Inténtalo en unos minutos."
    if error and error != "error_desconocido":
        return ERROR_MSGS.get(error, f"⚠️ Error: {error}")
    if event_data:
        missing = []
        if not event_data.get("title"): missing.append("título")
        if not event_data.get("datetime"): missing.append("fecha/hora")
        if not event_data.get("location"): missing.append("lugar")
        if missing:
            return f"ℹ️ Faltan datos: *{', '.join(missing)}*"
    return "ℹ️ No detecté un evento concreto"

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or not msg.text:
        return
    log.info(f"📨 Texto recibido (id={msg.message_id})")
    events, error = extract_from_text(msg.text)
    if not events:
        reason = build_ignore_reason(None, error)
        log.info(f"   → Ignorado: {reason}")
        if REVIEW_CHAT_ID:
            preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text=f"{reason}\n\n_{preview}_\n\nPuedes añadirlo manualmente desde el panel admin.",
                parse_mode="Markdown"
            )
        return
    for i, event_data in enumerate(events):
        log.info(f"   → Evento {i+1}/{len(events)}: {event_data.get('title')}")
        ok = add_event(event_data, source_id=f"{msg.message_id}_{i}")
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
    caption = msg.caption or ""

    # Intentar primero con la imagen
    events, error = extract_from_image(image_bytes, caption)

    # Si falla pero hay un pie de foto rico, intentar con el texto del pie
    if not events and len(caption) > 30:
        log.info("   → Imagen no procesada, intentando con el texto del pie de foto...")
        events, error = extract_from_text(caption)
        if events:
            log.info("   → Evento extraído del pie de foto")

    if not events:
        reason = build_ignore_reason(None, error)
        log.info(f"   → Ignorado: {reason}")
        if REVIEW_CHAT_ID:
            caption_info = f"\nPie de foto: _{caption}_" if caption else ""
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text=f"{reason}{caption_info}\n\nPuedes añadirlo manualmente desde el panel admin.",
                parse_mode="Markdown"
            )
        return


# ─── API KEY ──────────────────────────────────────────────────────────────────
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "165db66c54673e7b364301cf0f986a5761c9149d5da589139eb525bda7e89e19")

# ─── SERVIDOR WEB CON API ─────────────────────────────────────────────────────
class APIHandler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Key")

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        self.send_response(200); self._cors(); self.end_headers()
        self.wfile.write(b"Bot Agenda Tetuan OK")

    def do_POST(self):
        key = self.headers.get("X-Admin-Key", "")
        if key != ADMIN_API_KEY:
            self.send_response(401); self._cors(); self.end_headers()
            self.wfile.write(b'{"error":"Unauthorized"}'); return

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        action = self.path.strip("/")

        try:
            if action == "delete":
                event_id = body.get("id", "")
                events, sha = load_events()
                events = [e for e in events if e.get("id") != event_id]
                ok = save_events(events, sha)
                log.info(f"Admin borró evento {event_id}")
                resp = json.dumps({"ok": ok}).encode()
            elif action == "save":
                events = body.get("events", [])
                _, sha = load_events()
                ok = save_events(events, sha)
                log.info(f"Admin guardó {len(events)} eventos")
                resp = json.dumps({"ok": ok}).encode()
            elif action == "upload-image":
                img_b64  = body.get("image_b64", "")
                filename = body.get("filename", "manual.jpg")
                img_bytes = base64.standard_b64decode(img_b64)
                url = upload_image_to_github(img_bytes, filename)
                log.info(f"Admin subió imagen: {filename}")
                resp = json.dumps({"ok": bool(url), "url": url}).encode()
            else:
                resp = json.dumps({"error": "unknown action"}).encode()

            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(resp)

        except Exception as e:
            log.error(f"API error: {e}")
            self.send_response(500); self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass

def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), APIHandler)
    log.info(f"Servidor web + API en puerto {port}")
    server.serve_forever()

async def daily_cleanup(context):
    log.info("⏰ Limpieza diaria de imágenes...")
    cleanup_past_events()

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

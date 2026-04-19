import base64 as _b64, os as _os
for _env, _fname in [("GOOGLE_CREDENTIALS_B64","google_credentials.json")]:
    _val = _os.environ.get(_env)
    if _val and not _os.path.exists(_fname):
        with open(_fname,"wb") as _f: _f.write(_b64.b64decode(_val))

import os, logging, base64, json, re, pickle, inspect, io, tempfile
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from groq import Groq
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from supabase import create_client

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY      = os.environ["GROQ_API_KEY"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_KEY"]
RENDER_URL        = os.environ["RENDER_URL"]
GOOGLE_CREDS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
TIMEZONE          = os.environ.get("TIMEZONE", "Asia/Kolkata")
SCOPES            = ["https://www.googleapis.com/auth/calendar"]

groq_client = Groq(api_key=GROQ_API_KEY)
supabase    = create_client(SUPABASE_URL, SUPABASE_KEY)

SYSTEM_PROMPT = """You are a task/event extraction assistant.
Extract ALL calendar events, tasks, or appointments from the input.
Today's date: {today}
Return ONLY a valid JSON array (no markdown, no explanation). Each object must have:
- "title": short descriptive event title
- "description": extra context (can be "")
- "date": YYYY-MM-DD
- "start_time": HH:MM 24h or null
- "end_time": HH:MM 24h or null
- "all_day": true or false
Time hints: dinner=19:00, lunch=12:30, morning meeting=09:00, EOD=17:00
Return [] if nothing found."""

def prompt():
    today = datetime.now().strftime("%Y-%m-%d (%A)")
    return SYSTEM_PROMPT.format(today=today)

def parse(raw):
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)

def extract_text(text):
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role":"system","content":prompt()},{"role":"user","content":text}],
        temperature=0)
    return parse(r.choices[0].message.content)

def extract_image(image_bytes, mime_type):
    b64 = base64.b64encode(image_bytes).decode()
    r = groq_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{"role":"system","content":prompt()},{"role":"user","content":[
            {"type":"text","text":"Extract all tasks and events from this image:"},
            {"type":"image_url","image_url":{"url":f"data:{mime_type};base64,{b64}"}}]}],
        temperature=0)
    return parse(r.choices[0].message.content)

def extract_pdf(pdf_bytes):
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() for p in reader.pages)
    except Exception:
        text = pdf_bytes.decode("utf-8", errors="replace")
    return extract_text(text)

def extract_audio(audio_bytes, mime_type="audio/ogg"):
    suffix = "." + mime_type.split("/")[-1]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes); tmp = f.name
    try:
        with open(tmp,"rb") as af:
            t = groq_client.audio.transcriptions.create(file=(f"audio{suffix}",af), model="whisper-large-v3")
        return extract_text(t.text)
    finally:
        os.unlink(tmp)

# ── Supabase helpers ───────────────────────────────────────────────────────────
def get_user_creds(telegram_id: int):
    r = supabase.table("users").select("token_json").eq("telegram_id", telegram_id).execute()
    if not r.data: return None
    token_json = r.data[0]["token_json"]
    if not token_json: return None
    info = json.loads(token_json)
    creds = Credentials(
        token=info.get("token"),
        refresh_token=info.get("refresh_token"),
        token_uri=info.get("token_uri"),
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        scopes=info.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_user_creds(telegram_id, creds)
    return creds

def save_user_creds(telegram_id: int, creds: Credentials):
    token_json = json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    })
    existing = supabase.table("users").select("telegram_id").eq("telegram_id", telegram_id).execute()
    if existing.data:
        supabase.table("users").update({"token_json": token_json, "connected": True}).eq("telegram_id", telegram_id).execute()
    else:
        supabase.table("users").insert({"telegram_id": telegram_id, "token_json": token_json, "connected": True}).execute()

def get_calendar_service(telegram_id: int):
    creds = get_user_creds(telegram_id)
    if not creds: return None
    return build("calendar", "v3", credentials=creds)

# ── Google OAuth ───────────────────────────────────────────────────────────────
pending_flows = {}

def make_auth_url(telegram_id: int) -> str:
    flow = Flow.from_client_secrets_file(
        GOOGLE_CREDS_FILE,
        scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth/callback"
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=str(telegram_id)
    )
    pending_flows[str(telegram_id)] = flow
    return auth_url

# ── Calendar event ─────────────────────────────────────────────────────────────
def add_event(service, ev):
    date_str = ev["date"]
    start_time = ev.get("start_time")
    end_time = ev.get("end_time")
    all_day = ev.get("all_day", False) or not start_time
    reminder = {"useDefault":False,"overrides":[{"method":"popup","minutes":15}]}
    if all_day:
        body = {"summary":ev["title"],"description":ev.get("description",""),
                "start":{"date":date_str},"end":{"date":date_str},"reminders":reminder}
    else:
        if end_time: end_dt = f"{date_str}T{end_time}:00"
        else:
            sh,sm = map(int,start_time.split(":")); end_dt = f"{date_str}T{(sh+1)%24:02d}:{sm:02d}:00"
        body = {"summary":ev["title"],"description":ev.get("description",""),
                "start":{"dateTime":f"{date_str}T{start_time}:00","timeZone":TIMEZONE},
                "end":{"dateTime":end_dt,"timeZone":TIMEZONE},"reminders":reminder}
    r = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
    return r.get("htmlLink","")

# ── Download helper ────────────────────────────────────────────────────────────
async def download(bot, file_id):
    f = await bot.get_file(file_id)
    async with httpx.AsyncClient() as c:
        r = await c.get(f.file_path); r.raise_for_status(); return r.content

# ── Core processor ─────────────────────────────────────────────────────────────
async def process(update: Update, extractor, telegram_id: int):
    service = get_calendar_service(telegram_id)
    if not service:
        auth_url = make_auth_url(telegram_id)
        await update.message.reply_text(
            "👋 First, connect your Google Calendar:\n\n"
            f"[Click here to connect]({auth_url})\n\n"
            "After connecting, send your message again!",
            parse_mode="Markdown")
        return

    status = await update.message.reply_text("⏳ Analysing…")
    try:
        events = await extractor() if inspect.iscoroutinefunction(extractor) else extractor()
        if not events:
            await status.edit_text("🤷 No events found. Try: _'Team meeting Friday at 3pm'_", parse_mode="Markdown"); return
        await status.edit_text(f"📅 Found {len(events)} event(s). Adding to your Google Calendar…")
        lines = [f"✅ *Added {len(events)} event(s)!*\n"]
        for ev in events:
            link = add_event(service, ev)
            time_str = f" at {ev['start_time']}" if ev.get("start_time") else " _(all day)_"
            lines.append(f"• *{ev['title']}*  {ev['date']}{time_str}")
            if link: lines.append(f"  [Open in Calendar]({link})")
        await status.edit_text("\n".join(lines), parse_mode="Markdown")
    except json.JSONDecodeError:
        await status.edit_text("❌ AI returned unexpected format. Please try again.")
    except Exception as e:
        logger.exception("Error"); await status.edit_text(f"❌ Error: {e}")

# ── Telegram handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    creds = get_user_creds(tid)
    if creds:
        await update.message.reply_text(
            "👋 Welcome back! Your Google Calendar is connected.\n\nSend me:\n"
            "📝 *Text* — describe your tasks\n🎤 *Voice* — speak your tasks\n"
            "🖼 *Photo* — whiteboard, screenshot\n📄 *PDF* — meeting notes",
            parse_mode="Markdown")
    else:
        auth_url = make_auth_url(tid)
        await update.message.reply_text(
            "👋 *Welcome to Task-to-Calendar Bot!*\n\n"
            "I'll add tasks to your Google Calendar automatically.\n\n"
            "First, connect your Google Calendar:\n"
            f"[👉 Click here to connect]({auth_url})",
            parse_mode="Markdown")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text
    await process(update, lambda: extract_text(text), tid)

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    photo = update.message.photo[-1]
    async def ext():
        data = await download(context.bot, photo.file_id)
        return extract_image(data, "image/jpeg")
    await process(update, ext, tid)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    doc = update.message.document; mime = doc.mime_type or ""
    async def ext():
        data = await download(context.bot, doc.file_id)
        if "pdf" in mime: return extract_pdf(data)
        elif mime.startswith("image/"): return extract_image(data, mime)
        else: return extract_text(data.decode("utf-8", errors="replace"))
    await process(update, ext, tid)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    voice = update.message.voice or update.message.audio
    async def ext():
        data = await download(context.bot, voice.file_id)
        mime = getattr(voice,"mime_type",None) or "audio/ogg"
        return extract_audio(data, mime)
    await process(update, ext, tid)

# ── Web server (keep-alive + OAuth callback) ───────────────────────────────────
telegram_app = None

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if parsed.path == "/oauth/callback":
            params = parse_qs(parsed.query)
            state = params.get("state", [None])[0]
            code  = params.get("code",  [None])[0]
            if state and code and state in pending_flows:
                try:
                    flow = pending_flows.pop(state)
                    flow.fetch_token(code=code)
                    creds = flow.credentials
                    save_user_creds(int(state), creds)
                    self._respond(200, b"<h2>Connected! Go back to Telegram and send your tasks.</h2>")
                    if telegram_app:
                        import asyncio
                        asyncio.run_coroutine_threadsafe(
                            telegram_app.bot.send_message(
                                chat_id=int(state),
                                text="✅ Google Calendar connected! Now send me your tasks."
                            ), telegram_app.update_queue._loop if hasattr(telegram_app.update_queue, '_loop') else asyncio.get_event_loop()
                        )
                except Exception as e:
                    self._respond(500, f"<h2>Error: {e}</h2>".encode())
            else:
                self._respond(400, b"<h2>Invalid request</h2>")
        else:
            self._respond(200, b"OK")

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass

def main():
    global telegram_app
    Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), Handler).serve_forever(), daemon=True).start()
    logger.info("Web server started on port 8080")
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    telegram_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    logger.info("Bot running — multi-user mode")
    telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

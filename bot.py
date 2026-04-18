import base64 as _b64, os as _os
for _env, _fname in [("GOOGLE_CREDENTIALS_B64","google_credentials.json"),("GOOGLE_TOKEN_B64","google_token.pickle")]:
    _val = _os.environ.get(_env)
    if _val and not _os.path.exists(_fname):
        with open(_fname,"wb") as _f: _f.write(_b64.b64decode(_val))

import os, logging, base64, json, re, pickle, inspect, io
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN          = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY            = os.environ["GROQ_API_KEY"]
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
GOOGLE_TOKEN_FILE       = os.environ.get("GOOGLE_TOKEN_FILE", "google_token.pickle")
GOOGLE_CALENDAR_ID      = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
TIMEZONE                = os.environ.get("TIMEZONE", "Asia/Kolkata")
SCOPES                  = ["https://www.googleapis.com/auth/calendar"]

groq_client = Groq(api_key=GROQ_API_KEY)

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
    import tempfile
    suffix = "." + mime_type.split("/")[-1]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes); tmp = f.name
    try:
        with open(tmp,"rb") as af:
            t = groq_client.audio.transcriptions.create(file=(f"audio{suffix}",af), model="whisper-large-v3")
        return extract_text(t.text)
    finally:
        os.unlink(tmp)

def get_calendar():
    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        with open(GOOGLE_TOKEN_FILE,"rb") as f: creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE,"wb") as f: pickle.dump(creds, f)
    return build("calendar","v3",credentials=creds)

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

async def download(bot, file_id):
    f = await bot.get_file(file_id)
    async with httpx.AsyncClient() as c:
        r = await c.get(f.file_path); r.raise_for_status(); return r.content

async def process(update, extractor):
    status = await update.message.reply_text("⏳ Analysing…")
    try:
        events = await extractor() if inspect.iscoroutinefunction(extractor) else extractor()
        if not events:
            await status.edit_text("🤷 No events found. Try: _'Team meeting Friday at 3pm'_", parse_mode="Markdown"); return
        await status.edit_text(f"📅 Found {len(events)} event(s). Adding to Google Calendar…")
        service = get_calendar()
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

async def cmd_start(update, context):
    await update.message.reply_text(
        "👋 *Task-to-Calendar Bot*\n\nSend me:\n"
        "📝 *Text* — describe your tasks\n🎤 *Voice* — speak your tasks\n"
        "🖼 *Photo* — whiteboard, screenshot\n📄 *PDF* — meeting notes\n\n"
        "I'll add them to Google Calendar with 🔔 15-min reminders!", parse_mode="Markdown")

async def on_text(update, context):
    text = update.message.text
    await process(update, lambda: extract_text(text))

async def on_photo(update, context):
    photo = update.message.photo[-1]
    async def ext():
        data = await download(context.bot, photo.file_id)
        return extract_image(data, "image/jpeg")
    await process(update, ext)

async def on_document(update, context):
    doc = update.message.document; mime = doc.mime_type or ""
    async def ext():
        data = await download(context.bot, doc.file_id)
        if "pdf" in mime: return extract_pdf(data)
        elif mime.startswith("image/"): return extract_image(data, mime)
        else: return extract_text(data.decode("utf-8", errors="replace"))
    await process(update, ext)

async def on_voice(update, context):
    voice = update.message.voice or update.message.audio
    async def ext():
        data = await download(context.bot, voice.file_id)
        mime = getattr(voice,"mime_type",None) or "audio/ogg"
        return extract_audio(data, mime)
    await process(update, ext)

class _Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

def main():
    Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), _Ping).serve_forever(), daemon=True).start()
    logger.info("Keep-alive server started on port 8080")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    logger.info("Bot running — Groq (free tier)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

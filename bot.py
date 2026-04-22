import base64 as _b64, os as _os
for _env, _fname in [("GOOGLE_CREDENTIALS_B64","google_credentials.json")]:
    _val = _os.environ.get(_env)
    if _val and not _os.path.exists(_fname):
        with open(_fname,"wb") as _f: _f.write(_b64.b64decode(_val))

import os, logging, base64, json, re, pickle, inspect, io, tempfile
from datetime import datetime, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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
SCOPES            = ["https://www.googleapis.com/auth/calendar.events"]

groq_client = Groq(api_key=GROQ_API_KEY)
supabase    = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Token encryption (AES-256) ────────────────────────────────────────────────
from cryptography.fernet import Fernet
import base64 as _b64enc, hashlib as _hs

def _fernet():
    raw = os.environ.get("TOKEN_ENCRYPT_KEY", "")
    if not raw:
        raise ValueError("TOKEN_ENCRYPT_KEY env var not set")
    key = _b64enc.urlsafe_b64encode(_hs.sha256(raw.encode()).digest())
    return Fernet(key)

def encrypt_token(data: str) -> str:
    return _fernet().encrypt(data.encode()).decode()

def decrypt_token(data: str) -> str:
    return _fernet().decrypt(data.encode()).decode()


# ── Prompts ────────────────────────────────────────────────────────────────────
EXTRACT_PROMPT = """You are a task/event extraction assistant.
Extract ALL calendar events, tasks, or appointments from the input.

Today is {today}.

Resolve relative dates carefully:
- "tomorrow" = today + 1 day
- "next Monday" = the NEXT Monday that has not happened yet
- "this Friday" = the coming Friday this week
- Always calculate exact dates based on today

Return ONLY a valid JSON array (no markdown, no explanation). Each object must have:
- "title": short descriptive event title
- "description": extra context (can be "")
- "date": YYYY-MM-DD
- "start_time": HH:MM 24h or null
- "end_time": HH:MM 24h or null
- "all_day": true or false
- "recurring": null, "daily", "weekly", "monthly", or "weekdays"

Time hints: dinner=19:00, lunch=12:30, morning=09:00, EOD=17:00
Recurring hints: "every day"=daily, "every Monday"=weekly, "every weekday"=weekdays, "every month"=monthly

Return [] if nothing found."""

INTENT_PROMPT = """You are an intent classifier for a calendar bot.
Classify the user message into exactly one of these intents:

- ADD_EVENT: user wants to add, create, or schedule something
- EDIT_EVENT: user wants to change, update, reschedule, or rename an event
- DELETE_EVENT: user wants to delete, remove, or cancel an event
- DISCONNECT: user wants to sign out, disconnect, unlink, or remove their Google account
- STATUS: user wants to know if they are connected or check their account
- LIST_EVENTS: user wants to see, list, or check upcoming events
- HELP: user is asking for help or what the bot can do
- UNKNOWN: none of the above

Reply with ONLY the intent word, nothing else."""

EDIT_PROMPT = """You are a calendar event editor.
The user wants to edit an event. Here are their upcoming events:

{events}

User instruction: {instruction}

Return a JSON object with:
- "event_index": 0-based index of the event to edit (integer)
- "action": "reschedule", "rename", or "delete"
- "new_time": "HH:MM" in 24h format if rescheduling (or null)
- "new_date": "YYYY-MM-DD" if changing date (or null)
- "new_title": new title string if renaming (or null)

Return ONLY valid JSON, no markdown."""

def today_str():
    return datetime.now().strftime("%Y-%m-%d (%A)")

def parse_json(raw):
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)

# ── Groq helpers ───────────────────────────────────────────────────────────────
def detect_intent(text: str) -> str:
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": INTENT_PROMPT},
            {"role": "user", "content": text}
        ],
        temperature=0, max_tokens=20)
    return r.choices[0].message.content.strip().upper()

def extract_text(text: str) -> list:
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT.format(today=today_str())},
            {"role": "user", "content": text}
        ],
        temperature=0)
    return parse_json(r.choices[0].message.content)

def extract_image(image_bytes: bytes, mime_type: str) -> list:
    b64 = base64.b64encode(image_bytes).decode()
    r = groq_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{"role": "system", "content": EXTRACT_PROMPT.format(today=today_str())},
                  {"role": "user", "content": [
                      {"type": "text", "text": "Extract all tasks and events:"},
                      {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}]}],
        temperature=0)
    return parse_json(r.choices[0].message.content)

def extract_pdf(pdf_bytes: bytes) -> list:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() for p in reader.pages)
    except Exception:
        text = pdf_bytes.decode("utf-8", errors="replace")
    return extract_text(text)

def extract_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> tuple:
    """Returns (transcribed_text, events_list)"""
    suffix = "." + mime_type.split("/")[-1]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes); tmp = f.name
    try:
        with open(tmp, "rb") as af:
            t = groq_client.audio.transcriptions.create(
                file=(f"audio{suffix}", af), model="whisper-large-v3")
        transcribed = t.text
        return transcribed, extract_text(transcribed)
    finally:
        os.unlink(tmp)

def parse_edit_instruction(instruction: str, events: list) -> dict:
    event_list = "\n".join([
        f"{i}. {ev.get('summary','Untitled')} — {ev.get('start',{}).get('dateTime', ev.get('start',{}).get('date',''))[:16].replace('T',' ')}"
        for i, ev in enumerate(events)
    ])
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": EDIT_PROMPT.format(events=event_list, instruction=instruction)}],
        temperature=0)
    return parse_json(r.choices[0].message.content)

# ── Supabase helpers ───────────────────────────────────────────────────────────
def get_user_creds(telegram_id: int):
    r = supabase.table("users").select("token_json").eq("telegram_id", telegram_id).execute()
    if not r.data or not r.data[0].get("token_json"):
        return None
    info = json.loads(r.data[0]["token_json"])
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
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    })
    token_json = encrypt_token(token_json)
    existing = supabase.table("users").select("telegram_id").eq("telegram_id", telegram_id).execute()
    if existing.data:
        supabase.table("users").update({"token_json": token_json, "connected": True}).eq("telegram_id", telegram_id).execute()
    else:
        supabase.table("users").insert({"telegram_id": telegram_id, "token_json": token_json, "connected": True}).execute()

def disconnect_user(telegram_id: int) -> bool:
    existing = supabase.table("users").select("token_json").eq("telegram_id", telegram_id).execute()
    if existing.data:
        raw_token = existing.data[0].get("token_json", "")
        if raw_token:
            try:
                info = json.loads(decrypt_token(raw_token))
                refresh_token = info.get("refresh_token")
                if refresh_token:
                    import urllib.request, urllib.parse
                    data = urllib.parse.urlencode({"token": refresh_token}).encode()
                    req = urllib.request.Request(
                        "https://oauth2.googleapis.com/revoke", data=data,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
                    try:
                        urllib.request.urlopen(req, timeout=5)
                        logger.info(f"Google token revoked for {telegram_id}")
                    except Exception as e:
                        logger.warning(f"Google revoke failed: {e}")
            except Exception as e:
                logger.warning(f"Could not decrypt for revocation: {e}")
        supabase.table("users").update({"token_json": "", "connected": False}).eq("telegram_id", telegram_id).execute()
        return True
    return False

def get_calendar_service(telegram_id: int):
    creds = get_user_creds(telegram_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)

# ── OAuth ──────────────────────────────────────────────────────────────────────
pending_flows = {}

def make_auth_url(telegram_id: int) -> str:
    flow = Flow.from_client_secrets_file(
        GOOGLE_CREDS_FILE, scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth/callback")
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
        state=str(telegram_id))
    pending_flows[str(telegram_id)] = flow
    return auth_url

# ── Calendar helpers ───────────────────────────────────────────────────────────
def add_event(service, ev: dict) -> str:
    date_str   = ev["date"]
    start_time = ev.get("start_time")
    end_time   = ev.get("end_time")
    all_day    = ev.get("all_day", False) or not start_time
    reminder   = {"useDefault": False, "overrides": [
        {"method": "popup", "minutes": 15},
        {"method": "popup", "minutes": 5},
    ]}

    if all_day:
        body = {"summary": ev["title"], "description": ev.get("description", ""),
                "start": {"date": date_str}, "end": {"date": date_str}, "reminders": reminder}
    else:
        if end_time:
            end_dt = f"{date_str}T{end_time}:00"
        else:
            sh, sm = map(int, start_time.split(":"))
            end_dt = f"{date_str}T{(sh+1)%24:02d}:{sm:02d}:00"
        body = {"summary": ev["title"], "description": ev.get("description", ""),
                "start": {"dateTime": f"{date_str}T{start_time}:00", "timeZone": TIMEZONE},
                "end":   {"dateTime": end_dt, "timeZone": TIMEZONE},
                "reminders": reminder}

    recurring = ev.get("recurring")
    if recurring == "daily":    body["recurrence"] = ["RRULE:FREQ=DAILY"]
    elif recurring == "weekly": body["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    elif recurring == "monthly":body["recurrence"] = ["RRULE:FREQ=MONTHLY"]
    elif recurring == "weekdays":body["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"]

    r = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
    return r.get("htmlLink", "")

def get_upcoming_events(service, days: int = 7) -> list:
    now = datetime.utcnow().isoformat() + "Z"
    later = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"
    result = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID, timeMin=now, timeMax=later,
        maxResults=5, singleEvents=True, orderBy="startTime").execute()
    return result.get("items", [])

# ── Download helper ────────────────────────────────────────────────────────────
async def download(bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    async with httpx.AsyncClient() as c:
        r = await c.get(f.file_path); r.raise_for_status(); return r.content

# ── Intent handlers ────────────────────────────────────────────────────────────
async def handle_add(update: Update, extractor, telegram_id: int):
    service = get_calendar_service(telegram_id)
    if not service:
        auth_url = make_auth_url(telegram_id)
        await update.message.reply_text(
            "👋 First, connect your Google Calendar:\n\n"
            f"[Click here to connect]({auth_url})\n\n"
            "After connecting, send your message again!",
            parse_mode="Markdown")
        return

    status = await update.message.reply_text("⏳ Extracting events…")
    try:
        result = await extractor() if inspect.iscoroutinefunction(extractor) else extractor()
        if isinstance(result, tuple):
            events = result[1]
        else:
            events = result

        if not events:
            await status.edit_text(
                "🤷 No events found.\n\n"
                "Try being specific, e.g:\n"
                "_Dentist tomorrow at 3pm_\n"
                "_Team meeting every Monday 10am_",
                parse_mode="Markdown")
            return

        await status.edit_text(f"📅 Adding {len(events)} event(s) to your Google Calendar…")
        lines = [f"✅ *Added {len(events)} event(s)!*\n"]
        for ev in events:
            link = add_event(service, ev)
            time_str = f" at {ev['start_time']}" if ev.get("start_time") else " _(all day)_"
            recurring_map = {"daily": " 🔁 Daily", "weekly": " 🔁 Weekly",
                             "monthly": " 🔁 Monthly", "weekdays": " 🔁 Weekdays"}
            rec_str = recurring_map.get(ev.get("recurring"), "")
            lines.append(f"• *{ev['title']}*  {ev['date']}{time_str}{rec_str}")
            if link: lines.append(f"  [Open in Calendar]({link})")
        lines.append("\n_🔔 Reminders set for 15 min and 5 min before each event_")
        await status.edit_text("\n".join(lines), parse_mode="Markdown")

    except json.JSONDecodeError:
        await status.edit_text("❌ Couldn't parse the response. Please try again.")
    except Exception as e:
        logger.exception("Error in handle_add")
        await status.edit_text(f"❌ Error: {e}")

async def handle_disconnect(update: Update, telegram_id: int):
    disconnected = disconnect_user(telegram_id)
    if disconnected:
        await update.message.reply_text(
            "✅ Your Google Calendar has been disconnected.\n\n"
            "Send /start whenever you want to reconnect.")
    else:
        await update.message.reply_text(
            "You are not connected to any Google Calendar.\n"
            "Send /start to connect.")

async def handle_list(update: Update, telegram_id: int):
    service = get_calendar_service(telegram_id)
    if not service:
        auth_url = make_auth_url(telegram_id)
        await update.message.reply_text(
            f"Connect your Google Calendar first:\n[Click here]({auth_url})",
            parse_mode="Markdown")
        return

    events = get_upcoming_events(service, days=7)
    if not events:
        await update.message.reply_text("📅 No upcoming events in the next 7 days.")
        return

    lines = ["📅 *Your next events:*\n"]
    for ev in events:
        start = ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", ""))
        title = ev.get("summary", "Untitled")
        time_display = start[:16].replace("T", " at ") if "T" in start else start
        lines.append(f"• *{title}* — {time_display}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def handle_edit_or_delete(update: Update, text: str, telegram_id: int):
    service = get_calendar_service(telegram_id)
    if not service:
        auth_url = make_auth_url(telegram_id)
        await update.message.reply_text(
            f"Connect your Google Calendar first:\n[Click here]({auth_url})",
            parse_mode="Markdown")
        return

    status = await update.message.reply_text("⏳ Finding your events…")
    events = get_upcoming_events(service, days=14)
    if not events:
        await status.edit_text("📅 No upcoming events found in the next 14 days.")
        return

    try:
        edit_info = parse_edit_instruction(text, events)
        idx = edit_info.get("event_index", 0)
        action = edit_info.get("action", "")
        ev = events[idx]
        event_id = ev["id"]
        title = ev.get("summary", "Untitled")

        if action == "delete":
            service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
            await status.edit_text(f"🗑 *{title}* has been deleted.", parse_mode="Markdown")

        elif action == "rename":
            new_title = edit_info.get("new_title", title)
            ev["summary"] = new_title
            service.events().update(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=ev).execute()
            await status.edit_text(f"✅ Renamed to *{new_title}*", parse_mode="Markdown")

        elif action == "reschedule":
            new_time = edit_info.get("new_time")
            new_date = edit_info.get("new_date")
            current_start = ev.get("start", {}).get("dateTime", "")
            date_str = new_date or (current_start[:10] if current_start else datetime.now().strftime("%Y-%m-%d"))

            if new_time:
                h, m = map(int, new_time.split(":"))
                new_start = f"{date_str}T{h:02d}:{m:02d}:00"
                new_end = f"{date_str}T{(h+1)%24:02d}:{m:02d}:00"
                ev["start"] = {"dateTime": new_start, "timeZone": TIMEZONE}
                ev["end"]   = {"dateTime": new_end, "timeZone": TIMEZONE}
                service.events().update(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=ev).execute()
                await status.edit_text(
                    f"✅ *{title}* rescheduled to {date_str} at {h:02d}:{m:02d}",
                    parse_mode="Markdown")
            else:
                await status.edit_text("❌ Couldn't figure out the new time. Try: _'Change dentist to 4pm'_", parse_mode="Markdown")
        else:
            await status.edit_text("❌ Couldn't understand that edit. Try: _'Delete my 3pm meeting'_ or _'Change dentist to 4pm'_", parse_mode="Markdown")

    except Exception as e:
        logger.exception("Edit error")
        await status.edit_text(f"❌ Error: {e}")

# ── Telegram handlers ──────────────────────────────────────────────────────────
WELCOME_MESSAGE = """👋 *Welcome to CalendarBot!*

Send me your tasks and appointments in any format — I'll extract the details and add them to your Google Calendar automatically, with reminders 15 min and 5 min before every event.

*Ways to send tasks:*
📝 *Text* — type your task naturally
🎤 *Voice note* — speak it out loud
🖼 *Photo* — whiteboard, sticky note, handwritten list, screenshot
📄 *PDF* — meeting notes, documents, any file with dates

*Examples of what to say:*
- _"Dentist tomorrow at 3pm"_
- _"Team standup every Monday at 10am"_
- _"Submit report by Friday EOD"_
- _"Client dinner on April 25th"_

*Managing your events:*
- _"Change my dentist to 4pm"_
- _"Delete my Monday meeting"_
- _"Show my upcoming events"_
- _"Disconnect my Google account"_

First, connect your Google Calendar below 👇"""

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    creds = get_user_creds(tid)
    if creds:
        await update.message.reply_text(
            "👋 *Welcome back!*\n\n"
            "Your Google Calendar is connected. Just send me a message to add events!\n\n"
            "*Examples:*\n"
            "• _Dentist tomorrow at 3pm_\n"
            "• _Team standup every Monday 10am_\n"
            "• _Change my dentist to 4pm_\n"
            "• _Show my upcoming events_\n"
            "• _Disconnect my Google account_",
            parse_mode="Markdown")
    else:
        auth_url = make_auth_url(tid)
        await update.message.reply_text(
            WELCOME_MESSAGE + f"\n\n[👉 Connect Google Calendar]({auth_url})",
            parse_mode="Markdown")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text

    intent = detect_intent(text)
    logger.info(f"Intent: {intent} for: {text[:50]}")

    if intent == "DISCONNECT":
        await handle_disconnect(update, tid)
    elif intent in ("EDIT_EVENT", "DELETE_EVENT"):
        await handle_edit_or_delete(update, text, tid)
    elif intent == "LIST_EVENTS":
        await handle_list(update, tid)
    elif intent == "STATUS":
        creds = get_user_creds(tid)
        if creds:
            await update.message.reply_text("✅ Your Google Calendar is connected and ready!")
        else:
            auth_url = make_auth_url(tid)
            await update.message.reply_text(
                f"❌ Not connected. [Click here to connect]({auth_url})",
                parse_mode="Markdown")
    elif intent == "HELP":
        await update.message.reply_text(
            "🤖 *CalendarBot Help*\n\n"
            "Just talk to me naturally!\n\n"
            "*Adding events:*\n"
            "• _Dentist tomorrow at 3pm_\n"
            "• _Team meeting every Monday 10am_\n"
            "• _Submit report by Friday EOD_\n\n"
            "*Editing events:*\n"
            "• _Change my dentist to 4pm_\n"
            "• _Rename standup to Team Sync_\n"
            "• _Delete my Monday meeting_\n\n"
            "*Other:*\n"
            "• _Show my upcoming events_\n"
            "• _Am I connected?_\n"
            "• _Disconnect my Google account_\n\n"
            "You can also send voice messages, photos, or PDFs!",
            parse_mode="Markdown")
    else:
        await handle_add(update, lambda: extract_text(text), tid)

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    photo = update.message.photo[-1]
    async def ext():
        data = await download(context.bot, photo.file_id)
        return extract_image(data, "image/jpeg")
    await handle_add(update, ext, tid)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    doc = update.message.document
    mime = doc.mime_type or ""
    async def ext():
        data = await download(context.bot, doc.file_id)
        if "pdf" in mime: return extract_pdf(data)
        elif mime.startswith("image/"): return extract_image(data, mime)
        else: return extract_text(data.decode("utf-8", errors="replace"))
    await handle_add(update, ext, tid)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    voice = update.message.voice or update.message.audio

    # For voice, first detect intent from transcription
    status = await update.message.reply_text("🎤 Transcribing…")
    try:
        data = await download(context.bot, voice.file_id)
        mime = getattr(voice, "mime_type", None) or "audio/ogg"
        transcribed, events = extract_audio(data, mime)

        await status.edit_text(f"💬 _{transcribed}_", parse_mode="Markdown")

        # Detect intent from transcribed text
        intent = detect_intent(transcribed)
        logger.info(f"Voice intent: {intent}")

        if intent == "DISCONNECT":
            await handle_disconnect(update, tid)
        elif intent in ("EDIT_EVENT", "DELETE_EVENT"):
            await handle_edit_or_delete(update, transcribed, tid)
        elif intent == "LIST_EVENTS":
            await handle_list(update, tid)
        else:
            await handle_add(update, lambda: (transcribed, events), tid)

    except Exception as e:
        logger.exception("Voice error")
        await status.edit_text(f"❌ Error processing voice: {e}")

# ── Web server ─────────────────────────────────────────────────────────────────
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

            if state and code:
                if state not in pending_flows:
                    self._html(200,
                        "<h2>Session expired</h2>"
                        "<p>Go back to Telegram and send /start to get a new login link.</p>")
                    return
                try:
                    flow = pending_flows.pop(state)
                    flow.fetch_token(code=code)
                    save_user_creds(int(state), flow.credentials)
                    self._html(200,
                        "<h2 style='color:green'>&#10003; Connected!</h2>"
                        "<p>Your Google Calendar is now connected.</p>"
                        "<p><b>Go back to Telegram and send your first task!</b></p>")
                    if telegram_app:
                        import asyncio
                        future = asyncio.run_coroutine_threadsafe(
                            telegram_app.bot.send_message(
                                chat_id=int(state),
                                text="✅ Google Calendar connected!\n\nNow just send me your tasks. Try:\n_Dentist tomorrow at 3pm_",
                                parse_mode="Markdown"),
                            telegram_app.update_queue._loop if hasattr(telegram_app.update_queue, "_loop") else asyncio.new_event_loop()
                        )
                except Exception as e:
                    self._html(500, f"<h2>Error: {e}</h2>")
            else:
                self._html(400, "<h2>Invalid request</h2>")
            return

        self._html(200, "<h2>CalendarBot is running!</h2>")

    def _html(self, code, body):
        full = f"<html><body style='font-family:Arial;text-align:center;padding:60px;max-width:500px;margin:auto'>{body}</body></html>"
        encoded = full.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args): pass

# ── Main ───────────────────────────────────────────────────────────────────────
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

    logger.info("CalendarBot running — natural language mode")
    telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

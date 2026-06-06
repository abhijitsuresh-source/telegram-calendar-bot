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

# ── Optional env vars ─────────────────────────────────────────────────────────
RAZORPAY_KEY_ID      = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET  = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_BILLING_ENABLED = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

if RAZORPAY_BILLING_ENABLED:
    from razorpay_billing import (
        create_or_get_customer, create_subscription,
        cancel_subscription, verify_webhook_signature, parse_webhook, PLANS,
    )

# ── Rate limiting config ──────────────────────────────────────────────────────
RATE_LIMIT_MESSAGES  = 30   # max messages per window
RATE_LIMIT_WINDOW    = 60   # seconds

# ── Subscription helpers ──────────────────────────────────────────────────────

def get_subscription(telegram_id: int) -> dict | None:
    """Return the subscription row for a user, or None."""
    try:
        r = supabase.table("subscriptions").select("*").eq("telegram_id", telegram_id).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None

def ensure_trial(telegram_id: int):
    """Create a trial subscription row if one doesn't exist yet."""
    from datetime import timezone
    existing = get_subscription(telegram_id)
    if not existing:
        from datetime import datetime, timedelta
        now = datetime.now(timezone.utc)
        supabase.table("subscriptions").insert({
            "telegram_id":    telegram_id,
            "plan":           "free",
            "status":         "trial",
            "trial_started_at": now.isoformat(),
            "trial_ends_at":  (now + timedelta(days=7)).isoformat(),
        }).execute()

def is_subscribed(telegram_id: int) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    allowed=True  → user may use the bot.
    allowed=False → reason explains why not.
    """
    if not RAZORPAY_BILLING_ENABLED:
        return True, "billing_disabled"

    ensure_trial(telegram_id)
    sub = get_subscription(telegram_id)
    if not sub:
        return False, "no_subscription"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    status = sub.get("status", "trial")

    if status == "active":
        return True, "active"

    if status == "trial":
        trial_ends = sub.get("trial_ends_at")
        if trial_ends:
            # Parse ISO string — handle both Z and +00:00
            te = datetime.fromisoformat(trial_ends.replace("Z", "+00:00"))
            if now < te:
                days_left = (te - now).days
                return True, f"trial:{days_left}"
        return False, "trial_expired"

    return False, status  # cancelled / expired

def activate_subscription(telegram_id: int, razorpay_sub_id: str, plan: str):
    """Called from webhook when payment succeeds."""
    from datetime import datetime, timezone
    existing = get_subscription(telegram_id)
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        supabase.table("subscriptions").update({
            "status":           "active",
            "plan":             plan,
            "razorpay_sub_id":  razorpay_sub_id,
            "subscribed_at":    now,
            "cancelled_at":     None,
        }).eq("telegram_id", telegram_id).execute()
    else:
        supabase.table("subscriptions").insert({
            "telegram_id":      telegram_id,
            "status":           "active",
            "plan":             plan,
            "razorpay_sub_id":  razorpay_sub_id,
            "subscribed_at":    now,
        }).execute()

def deactivate_subscription(telegram_id: int):
    """Called from webhook when subscription is cancelled/expired."""
    from datetime import datetime, timezone
    supabase.table("subscriptions").update({
        "status":       "cancelled",
        "cancelled_at": datetime.now(timezone.utc).isoformat(),
    }).eq("telegram_id", telegram_id).execute()

# ── Rate limiter ──────────────────────────────────────────────────────────────

def check_rate_limit(telegram_id: int) -> bool:
    """
    Returns True if the user is within rate limits, False if exceeded.
    Uses a sliding window stored in Supabase.
    """
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        r = supabase.table("rate_limits").select("*").eq("telegram_id", telegram_id).execute()

        if not r.data:
            supabase.table("rate_limits").insert({
                "telegram_id":   telegram_id,
                "message_count": 1,
                "window_start":  now.isoformat(),
            }).execute()
            return True

        row = r.data[0]
        window_start = datetime.fromisoformat(row["window_start"].replace("Z", "+00:00"))
        count        = row["message_count"]

        # Reset window if expired
        if (now - window_start).total_seconds() > RATE_LIMIT_WINDOW:
            supabase.table("rate_limits").update({
                "message_count": 1,
                "window_start":  now.isoformat(),
            }).eq("telegram_id", telegram_id).execute()
            return True

        if count >= RATE_LIMIT_MESSAGES:
            return False

        supabase.table("rate_limits").update({
            "message_count": count + 1,
        }).eq("telegram_id", telegram_id).execute()
        return True
    except Exception as e:
        logger.warning(f"Rate limit check failed for {telegram_id}: {e}")
        return True  # fail open — don't block users on DB errors

# ── Token encryption (AES-256 + PBKDF2 key derivation) ───────────────────────
from cryptography.fernet import Fernet
import base64 as _b64enc, hashlib as _hs

# Per-token random salt (16 bytes) stored alongside the ciphertext.
# Format on disk: base64(salt) + ":" + base64(fernet_token)
# Legacy single-salt format is still readable for backwards compat.
_PBKDF2_ITERATIONS = 260_000          # OWASP 2023 recommendation for SHA-256
_LEGACY_SALT       = b"calendarbot-v1"  # only used to decrypt old tokens

import secrets as _secrets

def _make_fernet(salt: bytes) -> Fernet:
    """Derive a Fernet key from TOKEN_ENCRYPT_KEY + the given salt."""
    raw = os.environ.get("TOKEN_ENCRYPT_KEY", "")
    if not raw:
        raise ValueError("TOKEN_ENCRYPT_KEY env var not set")
    dk  = _hs.pbkdf2_hmac("sha256", raw.encode(), salt, _PBKDF2_ITERATIONS, dklen=32)
    key = _b64enc.urlsafe_b64encode(dk)
    return Fernet(key)

def encrypt_token(data: str) -> str:
    """
    Encrypt with a fresh random 16-byte salt per token.
    Stored format: <base64_salt>:<fernet_token>
    """
    salt = _secrets.token_bytes(16)
    token = _make_fernet(salt).encrypt(data.encode()).decode()
    return _b64enc.urlsafe_b64encode(salt).decode() + ":" + token

def decrypt_token(data: str) -> str:
    """
    Decrypt. Handles both:
      - New format: <base64_salt>:<fernet_token>
      - Legacy format: raw fernet token (uses old fixed salt)
    """
    if ":" in data:
        salt_b64, token = data.split(":", 1)
        salt = _b64enc.urlsafe_b64decode(salt_b64 + "==")
        return _make_fernet(salt).decrypt(token.encode()).decode()
    else:
        # Legacy: fixed salt — still readable, will be re-encrypted on next save
        return _make_fernet(_LEGACY_SALT).decrypt(data.encode()).decode()


# ── Prompts ────────────────────────────────────────────────────────────────────
EXTRACT_PROMPT = """You are a task/event extraction assistant.
Extract ALL calendar events, tasks, or appointments from the input.

Today is {today}.

Resolve relative dates carefully:
- "tomorrow" = today + 1 day
- "next Monday" = the NEXT Monday that has not yet happened
- "this Friday" = the coming Friday this week
- A bare day name with no qualifier (e.g. "Friday", "schedule for Friday", "Monday meeting") = the NEXT occurrence of that day that has not yet happened — NEVER today even if today is that day
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
- INVITE: user wants to invite someone to a meeting or event (mentions inviting, sharing, or a person's name alongside an event)
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

INVITE_PROMPT = """You are a calendar invite extractor.
The user wants to invite someone to an event.

Today is {today}.

Extract the invite details and return ONLY a valid JSON object (no markdown):
- "title": event title (string)
- "invitee_name": name of person being invited (string, or "Someone" if not mentioned)
- "date": YYYY-MM-DD
- "start_time": HH:MM 24h or null
- "end_time": HH:MM 24h or null (default 1 hour after start_time)
- "all_day": true or false
- "description": any extra context (string, can be "")

Resolve relative dates carefully:
- "tomorrow" = today + 1 day
- "next Monday" = the NEXT Monday that has not yet happened
- A bare day name with no qualifier (e.g. "Friday", "this Friday") = the NEXT occurrence of that day that has not yet happened — NEVER today even if today is that day

Time hints: dinner=19:00, lunch=12:30, morning=09:00, EOD=17:00
"""

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
def parse_invite(text: str) -> dict:
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": INVITE_PROMPT.format(today=today_str()) + "\nUser message: " + text}],
        temperature=0)
    return parse_json(r.choices[0].message.content)

def get_user_creds(telegram_id: int):
    r = supabase.table("users").select("token_json").eq("telegram_id", telegram_id).execute()
    if not r.data or not r.data[0].get("token_json"):
        return None
    raw = decrypt_token(r.data[0]["token_json"])
    info = json.loads(raw)
    # Always use the current client_id/client_secret from the credentials file,
    # not the potentially stale ones stored in Supabase. This prevents
    # invalid_client errors if the secret is ever rotated.
    with open(GOOGLE_CREDS_FILE) as f:
        gcreds = json.load(f)
    app = gcreds.get("web") or gcreds.get("installed") or {}
    creds = Credentials(
        token=None,                        # never persist access token
        refresh_token=info.get("refresh_token"),
        token_uri=app.get("token_uri", info.get("token_uri")),
        client_id=app.get("client_id", info.get("client_id")),
        client_secret=app.get("client_secret", info.get("client_secret")),
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
        # Cancel all pending invites from this organiser immediately
        supabase.table("invites").update({"status": "cancelled"}).eq(
            "organiser_telegram_id", telegram_id).eq("status", "pending").execute()
        logger.info(f"Cancelled all pending invites for {telegram_id}")
        return True
    return False

def create_invite(organiser_id: int, event_data: dict, event_link: str) -> str:
    import secrets
    token = secrets.token_urlsafe(16)
    supabase.table("invites").insert({
        "token": token,
        "organiser_telegram_id": organiser_id,
        "event_data": json.dumps(event_data),
        "event_link": event_link,
        "status": "pending",
        "invitee_name": event_data.get("invitee_name", "Someone"),
    }).execute()
    return token

def get_invite(token: str) -> dict | None:
    r = supabase.table("invites").select("*").eq("token", token).execute()
    return r.data[0] if r.data else None

def is_invite_valid(invite: dict) -> tuple[bool, str]:
    """Returns (is_valid, reason). Checks organiser connection, status, and event date."""
    if not invite or invite["status"] != "pending":
        return False, "This invite link has already been used or doesn't exist."
    # Check organiser is still connected
    organiser_id = invite["organiser_telegram_id"]
    r = supabase.table("users").select("connected").eq("telegram_id", organiser_id).execute()
    if not r.data or not r.data[0].get("connected"):
        return False, "The organiser has disconnected their Google Calendar. This invite is no longer valid."
    # Check event date has not passed
    try:
        ev = json.loads(invite["event_data"])
        event_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        if event_date < datetime.now().date():
            return False, "This event has already passed. The invite is no longer valid."
    except Exception:
        pass
    return True, ""

def get_pending_invites(organiser_id: int) -> list:
    r = supabase.table("invites").select("*").eq(
        "organiser_telegram_id", organiser_id).eq("status", "pending").execute()
    return r.data or []

def update_invite(token: str, status: str, counter_time: str = None, counter_date: str = None, invitee_name: str = None):
    data = {"status": status}
    if counter_time: data["counter_time"] = counter_time
    if counter_date: data["counter_date"] = counter_date
    if invitee_name: data["invitee_name"] = invitee_name
    supabase.table("invites").update(data).eq("token", token).execute()

def get_calendar_service(telegram_id: int):
    creds = get_user_creds(telegram_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)

# ── OAuth ──────────────────────────────────────────────────────────────────────
# OAuth flows stored in Supabase instead of memory
# so server restarts don't break mid-auth users
import pickle as _pickle

def _save_oauth_flow(key: str, flow, is_invite: bool = False):
    """Persist OAuth flow to Supabase (serialised + encrypted)."""
    try:
        raw = _pickle.dumps(flow)
        encrypted = encrypt_token(_b64enc.urlsafe_b64encode(raw).decode())
        table = "oauth_flows"
        supabase.table(table).upsert({
            "key":        key,
            "flow_data":  encrypted,
            "is_invite":  is_invite,
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"Failed to save OAuth flow: {e}")

def _load_oauth_flow(key: str):
    """Load and delete OAuth flow from Supabase."""
    try:
        r = supabase.table("oauth_flows").select("flow_data").eq("key", key).execute()
        if not r.data:
            return None
        encrypted = r.data[0]["flow_data"]
        raw = _b64enc.urlsafe_b64decode(decrypt_token(encrypted) + "==")
        flow = _pickle.loads(raw)
        supabase.table("oauth_flows").delete().eq("key", key).execute()
        return flow
    except Exception as e:
        logger.error(f"Failed to load OAuth flow: {e}")
        return None

# Fallback in-memory store (used if Supabase is unavailable)
pending_flows = {}
pending_invite_flows = {}

def make_auth_url(telegram_id: int) -> str:
    flow = Flow.from_client_secrets_file(
        GOOGLE_CREDS_FILE, scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth/callback")
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
        state=str(telegram_id))
    _save_oauth_flow(str(telegram_id), flow, is_invite=False)
    pending_flows[str(telegram_id)] = flow  # fallback
    return auth_url

# ── Calendar helpers ───────────────────────────────────────────────────────────
def make_invite_auth_url(token: str) -> str:
    flow = Flow.from_client_secrets_file(
        GOOGLE_CREDS_FILE, scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/invite/callback")
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="select_account consent",
        state=token)
    _save_oauth_flow(token, flow, is_invite=True)
    pending_invite_flows[token] = flow  # fallback
    return auth_url

def add_event(service, ev: dict, with_name: str = None) -> str:
    date_str   = ev["date"]
    start_time = ev.get("start_time")
    end_time   = ev.get("end_time")
    all_day    = ev.get("all_day", False) or not start_time
    reminder   = {"useDefault": False, "overrides": [
        {"method": "popup", "minutes": 15},
        {"method": "popup", "minutes": 5},
    ]}

    base_description = ev.get("description", "")
    if with_name:
        description = f"With: {with_name}" + (f"\n\n{base_description}" if base_description else "")
    else:
        description = base_description

    if all_day:
        body = {"summary": ev["title"], "description": description,
                "start": {"date": date_str}, "end": {"date": date_str}, "reminders": reminder}
    else:
        if end_time:
            end_dt = f"{date_str}T{end_time}:00"
        else:
            sh, sm = map(int, start_time.split(":"))
            end_dt = f"{date_str}T{(sh+1)%24:02d}:{sm:02d}:00"
        body = {"summary": ev["title"], "description": description,
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
    import zoneinfo
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    later = (datetime.now(tz) + timedelta(days=days)).isoformat()
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
async def handle_invite(update: Update, text: str, telegram_id: int):
    service = get_calendar_service(telegram_id)
    if not service:
        auth_url = make_auth_url(telegram_id)
        await update.message.reply_text(
            f"First connect your Google Calendar:\n[Click here to connect]({auth_url})",
            parse_mode="Markdown")
        return

    status = await update.message.reply_text("⏳ Creating your invite…")
    try:
        invite_data = parse_invite(text)
        invitee_name = invite_data.get("invitee_name", "Someone")
        organiser_name = update.effective_user.first_name or "Organiser"
        invite_data["organiser_name"] = organiser_name

        # Store invite — event is only added to both calendars after invitee accepts
        token = create_invite(telegram_id, invite_data, "")
        invite_url = f"{RENDER_URL}/invite/{token}"

        date_str = invite_data.get("date", "")
        time_str = f" at {invite_data['start_time']}" if invite_data.get("start_time") else ""

        await status.edit_text(
            f"✅ *Invite created!*\n\n"
            f"• *{invite_data['title']}* — {date_str}{time_str}\n\n"
            f"📨 *Share this invite link with {invitee_name}:*\n"
            f"{invite_url}\n\n"
            f"_The event will be added to both calendars once they accept._\n"
            f"_I'll notify you when they respond._",
            parse_mode="Markdown")

    except Exception as e:
        logger.exception("Invite error")
        await status.edit_text(f"❌ Error creating invite: {e}")


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
    import zoneinfo
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    for ev in events:
        start = ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", ""))
        title = ev.get("summary", "Untitled")
        if "T" in start:
            # Parse the ISO datetime (handles both +05:30 and Z suffixes) and convert to local tz
            from datetime import timezone
            dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz)
            time_display = dt.strftime("%Y-%m-%d at %H:%M")
        else:
            time_display = start  # all-day event, just show the date
        lines.append(f"• *{title}* — {time_display}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

CANCEL_INVITE_PROMPT = """You are a calendar invite canceller.
The user wants to cancel a pending invite. Here are their pending invites:

{invites}

User instruction: {instruction}

Return a JSON object with:
- "invite_index": 0-based index of the invite to cancel (integer)

Return ONLY valid JSON, no markdown."""

def parse_cancel_invite_instruction(instruction: str, invites: list) -> dict:
    invite_list = "\n".join([
        f"{i}. {json.loads(inv['event_data']).get('title','Untitled')} — "
        f"{json.loads(inv['event_data']).get('date','')} "
        f"(invited: {inv.get('invitee_name','Someone')})"
        for i, inv in enumerate(invites)
    ])
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": CANCEL_INVITE_PROMPT.format(
            invites=invite_list, instruction=instruction)}],
        temperature=0)
    return parse_json(r.choices[0].message.content)

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
    pending_invites = get_pending_invites(telegram_id)

    if not events and not pending_invites:
        await status.edit_text("📅 No upcoming events or pending invites found.")
        return

    try:
        # Try to match against calendar events first
        edit_info = parse_edit_instruction(text, events) if events else {"event_index": -1, "action": ""}
        action = edit_info.get("action", "")
        idx = edit_info.get("event_index", 0)

        # If it's a delete and we have pending invites, check if it refers to an invite
        if action == "delete" and pending_invites:
            # Ask Groq to try matching against invites too
            cancel_info = parse_cancel_invite_instruction(text, pending_invites)
            invite_idx = cancel_info.get("invite_index", -1)

            # Simple heuristic: if the text mentions "invite" or a name that matches
            # an invitee, prefer the invite match
            text_lower = text.lower()
            invite_keywords = ["invite", "invitation"]
            invitee_names = [inv.get("invitee_name", "").lower() for inv in pending_invites]
            prefers_invite = (
                any(kw in text_lower for kw in invite_keywords) or
                any(name and name in text_lower for name in invitee_names)
            )

            if prefers_invite and 0 <= invite_idx < len(pending_invites):
                inv = pending_invites[invite_idx]
                ev_data = json.loads(inv["event_data"])
                inv_title = ev_data.get("title", "Untitled")
                inv_name = inv.get("invitee_name", "Someone")
                supabase.table("invites").update({"status": "cancelled"}).eq(
                    "token", inv["token"]).execute()
                await status.edit_text(
                    f"🗑 Invite for *{inv_title}* with *{inv_name}* has been cancelled.",
                    parse_mode="Markdown")
                return

        ev = events[idx] if events and 0 <= idx < len(events) else None
        if not ev:
            await status.edit_text("❌ Couldn't find the event. Try being more specific.")
            return

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

Your AI-powered Google Calendar assistant — add, edit, and share events just by chatting.

*Send events in any format:*
📝 *Text* — _"Dentist tomorrow at 3pm"_ or _"Team standup every Monday at 10am"_
🎤 *Voice note* — speak naturally, I'll transcribe and extract the event
🖼 *Photo* — whiteboard, sticky note, handwritten list, screenshot
📄 *PDF* — meeting notes, documents, any file with dates

*What I can do:*
📅 Add one-off and recurring events (daily, weekly, monthly, weekdays)
⏰ Set reminders automatically — 15 min and 5 min before every event
✏️ Edit events — _"Move my dentist to 4pm"_
🗑 Delete events — _"Cancel my Monday meeting"_
📋 List upcoming events — _"Show my schedule"_
📨 Invite people to meetings — _"Invite Rahul to my 3pm call"_
🔄 Let invitees counter-propose a different time

*Try saying:*
- _"Submit report by Friday EOD"_
- _"Client dinner on April 25th at 8pm"_
- _"Every weekday at 9am — morning standup"_
- _"Invite Priya to my Tuesday meeting"_

First, connect your Google Calendar below 👇

_Need help? [WhatsApp us](https://wa.me/919496784855) · Call +919496784855 · abhijitsuresh99@gmail.com_"""

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    ensure_trial(tid)
    allowed, reason = is_subscribed(tid)
    creds = get_user_creds(tid)

    # Build subscription status line
    if reason == "billing_disabled":
        sub_line = ""
    elif reason == "active":
        sub_line = "\n✅ *Subscription active*"
    elif reason.startswith("trial:"):
        days = reason.split(":")[1]
        sub_line = f"\n⏳ *Free trial — {days} day(s) left* · /subscribe to continue"
    elif reason == "trial_expired":
        sub_line = "\n🔒 *Trial expired* · Use /subscribe to unlock"
    else:
        sub_line = "\n🔒 *Subscription inactive* · Use /subscribe to reactivate"

    if creds:
        await update.message.reply_text(
            "👋 *Welcome back!*\n\n"
            "Your Google Calendar is connected. Send me anything — text, voice, photo, or PDF!\n\n"
            "*Examples:*\n"
            "• _Dentist tomorrow at 3pm_\n"
            "• _Every weekday at 9am — standup_\n"
            "• _Move my dentist to 4pm_\n"
            "• _Invite Rahul to my Tuesday meeting_\n"
            "• _Show my upcoming events_" + sub_line,
            parse_mode="Markdown")
    else:
        auth_url = make_auth_url(tid)
        await update.message.reply_text(
            WELCOME_MESSAGE + f"\n\n[👉 Connect Google Calendar]({auth_url})" + sub_line,
            parse_mode="Markdown")

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show subscription plans and payment links."""
    tid  = update.effective_user.id
    args = context.args  # e.g. /subscribe pro

    if not RAZORPAY_BILLING_ENABLED:
        await update.message.reply_text("💳 Billing is not configured yet. Check back soon!")
        return

    # Check if already active
    allowed, reason = is_subscribed(tid)
    if reason == "active":
        await update.message.reply_text(
            "✅ You already have an active subscription!\n\n"
            "Use /cancel to cancel at end of billing cycle.",
            parse_mode="Markdown")
        return

    plan = (args[0].lower() if args else None)
    if plan not in PLANS:
        # Show plan picker
        plan_lines = []
        for key, p in PLANS.items():
            features = " · ".join(p["features"])
            plan_lines.append(f"*{p['label']}* — ₹{p['price']}/month\n_{features}_\n👉 /subscribe {key}")
        await update.message.reply_text(
            "💳 *Choose a plan:*\n\n" + "\n\n".join(plan_lines),
            parse_mode="Markdown")
        return

    await update.message.reply_text("⏳ Generating your payment link…")
    customer_id = create_or_get_customer(tid)
    sub_id, payment_url = create_subscription(plan, customer_id, tid)

    if not payment_url:
        await update.message.reply_text(
            "❌ Could not create payment link. Please try again or contact support.")
        return

    plan_config = PLANS[plan]
    await update.message.reply_text(
        f"💳 *{plan_config['label']} Plan — ₹{plan_config['price']}/month*\n\n"
        f"Click the link below to complete payment.\n"
        f"Your bot access will activate instantly after payment.\n\n"
        f"[👉 Pay ₹{plan_config['price']} via Razorpay]({payment_url})\n\n"
        f"_Link expires in 15 minutes._",
        parse_mode="Markdown",
        disable_web_page_preview=True)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel subscription at end of billing cycle."""
    tid = update.effective_user.id

    if not RAZORPAY_BILLING_ENABLED:
        await update.message.reply_text("Billing is not configured.")
        return

    sub = get_subscription(tid)
    if not sub or sub.get("status") != "active":
        await update.message.reply_text("You don't have an active subscription to cancel.")
        return

    razorpay_sub_id = sub.get("razorpay_sub_id")
    if razorpay_sub_id:
        success = cancel_subscription(razorpay_sub_id)
        if success:
            await update.message.reply_text(
                "✅ Your subscription has been cancelled and will not renew.\n\n"
                "You can continue using the bot until the end of your current billing period.",
                parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "❌ Could not cancel via Razorpay. Please contact support.")
    else:
        deactivate_subscription(tid)
        await update.message.reply_text("Your subscription has been cancelled.")

async def _check_access(update: Update) -> bool:
    """
    Returns True if user is allowed to proceed.
    Sends a message and returns False if not.
    """
    tid = update.effective_user.id

    # Rate limit check
    if not check_rate_limit(tid):
        await update.message.reply_text(
            "⚠️ You're sending messages too fast. Please slow down a little.")
        return False

    # Subscription check
    allowed, reason = is_subscribed(tid)
    if not allowed:
        if reason == "trial_expired":
            await update.message.reply_text(
                "⏳ *Your free trial has ended.*\n\n"
                "Subscribe to continue using CalendarBot:\n\n"
                "👉 /subscribe",
                parse_mode="Markdown")
        elif reason in ("cancelled", "expired"):
            await update.message.reply_text(
                "🔒 *Your subscription is inactive.*\n\n"
                "Reactivate it with /subscribe",
                parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "🔒 *Access restricted.*\n\nUse /subscribe to get started.",
                parse_mode="Markdown")
        return False
    return True

async def send_help_message(update, telegram_id: int, unknown: bool = False):
    """Send a rich help message with plan-aware subscription info and support buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    allowed, reason = is_subscribed(telegram_id)

    # Subscription status block
    if reason == "billing_disabled":
        sub_block = ""
    elif reason == "active":
        plan = "basic"
        try:
            row = supabase.table("subscriptions").select("plan").eq("telegram_id", telegram_id).maybe_single().execute()
            if row.data:
                plan = row.data.get("plan", "basic")
        except Exception:
            pass

        if plan == "pro":
            sub_block = (
                "\n\n\U0001f451 *You're on the Pro plan \u2014 the best of CalendarBot!*\n"
                "You have full access to every feature including calendar invites, "
                "invite responses, and priority support. You're all set!"
            )
        else:
            sub_block = (
                "\n\n\u2b06\ufe0f *Upgrade to Pro \u2014 \u20b9499/month*\n"
                "Unlock calendar invites, let invitees accept/decline or suggest a different time, "
                "and get priority support.\n"
                "\U0001f449 /subscribe pro"
            )
    elif reason.startswith("trial:"):
        days = reason.split(":")[1]
        sub_block = (
            f"\n\n\u23f3 *Free trial \u2014 {days} day(s) left*\n"
            "Subscribe to keep using CalendarBot after your trial ends.\n"
            "\U0001f449 /subscribe"
        )
    elif reason == "trial_expired":
        sub_block = (
            "\n\n\U0001f512 *Your free trial has ended.*\n"
            "Subscribe to continue using CalendarBot.\n"
            "\U0001f449 /subscribe"
        )
    else:
        sub_block = (
            "\n\n\U0001f512 *Subscription inactive.*\n"
            "Reactivate to keep using CalendarBot.\n"
            "\U0001f449 /subscribe"
        )

    intro = (
        "\U0001f914 *Not sure what you meant.* Here's what I can do:\n\n"
        if unknown else
        "\U0001f4c5 *CalendarBot Help*\n\nJust talk to me naturally!\n\n"
    )

    text = (
        intro +
        "*\u2795 Adding events:*\n"
        "\u2022 _Dentist tomorrow at 3pm_\n"
        "\u2022 _Team standup every Monday at 10am_\n"
        "\u2022 _Submit report by Friday EOD_\n"
        "\u2022 Send a *voice note*, *photo*, or *PDF* too!\n\n"
        "*\u270f\ufe0f Editing & deleting:*\n"
        "\u2022 _Change my dentist to 4pm_\n"
        "\u2022 _Rename standup to Team Sync_\n"
        "\u2022 _Delete my Monday meeting_\n\n"
        "*\U0001f4cb Other:*\n"
        "\u2022 _Show my upcoming events_\n"
        "\u2022 _Am I connected?_\n"
        "\u2022 _Disconnect my Google account_" +
        sub_block
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f4ac WhatsApp", url="https://wa.me/919496784855"),
            InlineKeyboardButton("\U0001f4de Call", url="tel:+919496784855"),
            InlineKeyboardButton("\u2709\ufe0f Email", url="mailto:abhijitsuresh99@gmail.com"),
        ],
    ])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text

    if not await _check_access(update):
        return

    intent = detect_intent(text)
    logger.info(f"Intent: {intent} for: {text[:50]}")

    if intent == "INVITE":
        await handle_invite(update, text, tid)
    elif intent == "DISCONNECT":
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
        await send_help_message(update, tid)
    elif intent == "UNKNOWN":
        await send_help_message(update, tid, unknown=True)
    else:
        await handle_add(update, lambda: extract_text(text), tid)

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not await _check_access(update):
        return
    photo = update.message.photo[-1]
    async def ext():
        data = await download(context.bot, photo.file_id)
        return extract_image(data, "image/jpeg")
    await handle_add(update, ext, tid)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not await _check_access(update):
        return
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
    if not await _check_access(update):
        return
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

        if intent == "INVITE":
            await handle_invite(update, transcribed, tid)
        elif intent == "DISCONNECT":
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

# ── Invite page HTML helpers ──────────────────────────────────────────────────
def _invite_page(title, date_str, time_str, invitee_name, token, auth_url):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>CalendarBot Invite</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'DM Sans',sans-serif;background:#f0f4ff;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
    .card{{background:white;border-radius:24px;padding:40px;max-width:480px;width:100%;box-shadow:0 8px 40px rgba(0,0,0,0.08)}}
    .logo{{font-size:2.5rem;margin-bottom:16px}}
    .badge{{display:inline-block;background:#eff4ff;color:#2563eb;border:1px solid #c7d2fe;border-radius:100px;padding:4px 12px;font-size:0.75rem;font-weight:600;margin-bottom:20px}}
    h1{{font-family:'DM Serif Display',serif;font-size:1.6rem;color:#0f1117;margin-bottom:8px;line-height:1.2}}
    .meta{{color:#6b7280;font-size:0.9rem;margin-bottom:28px}}
    .event-box{{background:#f8faff;border:1px solid #e0e7ff;border-radius:14px;padding:20px;margin-bottom:24px}}
    .event-title{{font-weight:600;font-size:1.05rem;color:#0f1117;margin-bottom:6px}}
    .event-time{{color:#4b5563;font-size:0.875rem}}
    .btn{{display:block;width:100%;padding:14px;border-radius:12px;font-size:1rem;font-weight:500;text-align:center;text-decoration:none;cursor:pointer;border:none;transition:all 0.2s;margin-bottom:10px}}
    .btn-accept{{background:#2563eb;color:white;box-shadow:0 4px 12px rgba(37,99,235,0.25)}}
    .btn-accept:hover{{background:#1d4ed8}}
    .btn-counter{{background:white;color:#2563eb;border:1px solid #c7d2fe}}
    .btn-counter:hover{{background:#eff4ff}}
    .btn-decline{{background:white;color:#6b7280;border:1px solid #e5e7eb;font-size:0.875rem}}
    .counter-form{{display:none;margin-top:16px;padding:16px;background:#f9fafb;border-radius:12px;border:1px solid #e5e7eb}}
    .counter-form.show{{display:block}}
    label{{font-size:0.825rem;font-weight:500;color:#374151;display:block;margin-bottom:4px}}
    input{{width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:0.9rem;margin-bottom:12px;font-family:inherit}}
    .powered{{text-align:center;font-size:0.75rem;color:#9ca3af;margin-top:20px}}
  </style>
</head>
<body>
<div class="card">
  <div class="logo">📅</div>
  <div class="badge">CalendarBot Invite</div>
  <h1>You've been invited!</h1>
  <p class="meta">Someone wants to meet with you.</p>

  <div class="event-box">
    <div class="event-title">📌 {title}</div>
    <div class="event-time">🗓 {date_str} {time_str}</div>
  </div>

  <a href="{auth_url}" class="btn btn-accept">✅ Accept &amp; Add to Google Calendar</a>
  <button class="btn btn-counter" onclick="document.getElementById('cf').classList.toggle('show')">💬 Suggest a different time</button>

  <form class="counter-form" id="cf" action="/invite/counter/{token}" method="GET">
    <label>Your name</label>
    <input name="name" placeholder="e.g. Sarah" required/>
    <label>Suggest a date</label>
    <input name="date" type="date" required/>
    <label>Suggest a time</label>
    <input name="time" type="time" required/>
    <button type="submit" class="btn btn-accept" style="margin-bottom:0">Send Counter Proposal</button>
  </form>

  <a href="/invite/decline/{token}" class="btn btn-decline" style="margin-top:8px">✕ Decline</a>

  <div class="powered">Powered by CalendarBot</div>
</div>
</body>
</html>"""

def _simple_page(emoji, heading, body):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>CalendarBot</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500&display=swap" rel="stylesheet"/>
  <style>
    body{{font-family:'DM Sans',sans-serif;background:#f0f4ff;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
    .card{{background:white;border-radius:24px;padding:48px 40px;max-width:420px;width:100%;box-shadow:0 8px 40px rgba(0,0,0,0.08);text-align:center}}
    .icon{{font-size:3rem;margin-bottom:16px}}
    h2{{font-size:1.4rem;color:#0f1117;margin-bottom:8px}}
    p{{color:#6b7280;font-size:0.9rem;line-height:1.6}}
  </style>
</head>
<body>
<div class="card">
  <div class="icon">{emoji}</div>
  <h2>{heading}</h2>
  <p>{body}</p>
</div>
</body>
</html>"""


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

        # ── Payment success redirect ──────────────────────────────────────────
        if parsed.path == "/payment/success":
            self._html(200, _simple_page(
                "🎉", "Payment Successful!",
                "Your CalendarBot subscription is now active.<br><br>"
                "<strong>Go back to Telegram</strong> and start using the bot!"))
            return

        if parsed.path.startswith("/invite/") and not any(
            parsed.path.startswith(f"/invite/{x}") for x in ["callback", "counter", "decline"]
        ):
            token = parsed.path.split("/invite/")[-1].strip("/")
            invite = get_invite(token)
            valid, reason = is_invite_valid(invite)
            if not valid:
                self._html(404, _simple_page("❌", "Invite not available", reason))
                return
            ev = json.loads(invite["event_data"])
            auth_url = make_invite_auth_url(token)
            time_str = f"at {ev['start_time']}" if ev.get("start_time") else "(all day)"
            page = _invite_page(
                title=ev.get("title", "Meeting"),
                date_str=ev.get("date", ""),
                time_str=time_str,
                invitee_name=ev.get("invitee_name", "You"),
                token=token,
                auth_url=auth_url,
            )
            self._html(200, page)
            return

        # ── Invite: OAuth callback after invitee connects Google ──────────────
        if parsed.path == "/invite/callback":
            params = parse_qs(parsed.query)
            token = params.get("state", [None])[0]
            code  = params.get("code",  [None])[0]
            if not token or not code:
                self._html(400, _simple_page("❌", "Invalid request", "Something went wrong. Please try the link again."))
                return
            invite = get_invite(token)
            valid, reason = is_invite_valid(invite)
            if not valid:
                self._html(404, _simple_page("❌", "Invite not available", reason))
                return
            # Load from Supabase first, fall back to memory
            flow = _load_oauth_flow(token) or pending_invite_flows.pop(token, None)
            if not flow:
                self._html(400, _simple_page("⏱", "Session expired",
                    "Your session expired. Please go back and click Accept again."))
                return
            try:
                pass  # flow already loaded above
                flow.fetch_token(code=code)
                invitee_creds = flow.credentials
                invitee_service = build("calendar", "v3", credentials=invitee_creds)
                ev = json.loads(invite["event_data"])
                organiser_id = invite["organiser_telegram_id"]
                invitee_name = invite.get("invitee_name") or "Someone"
                organiser_name = ev.get("organiser_name") or "Organiser"

                # Add event to invitee's calendar — show organiser's name
                add_event(invitee_service, ev, with_name=organiser_name)

                # Immediately revoke invitee's Google access — we don't need it anymore
                try:
                    import urllib.request, urllib.parse
                    revoke_token = invitee_creds.refresh_token or invitee_creds.token
                    if revoke_token:
                        data = urllib.parse.urlencode({"token": revoke_token}).encode()
                        req = urllib.request.Request(
                            "https://oauth2.googleapis.com/revoke", data=data,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
                        urllib.request.urlopen(req, timeout=5)
                        logger.info(f"Invitee Google token revoked after event add")
                except Exception as e:
                    logger.warning(f"Invitee token revoke failed: {e}")

                # Add event to organiser's calendar — show invitee's name
                organiser_service = get_calendar_service(organiser_id)
                if organiser_service:
                    organiser_link = add_event(organiser_service, ev, with_name=invitee_name)
                    supabase.table("invites").update({"event_link": organiser_link}).eq("token", token).execute()
                else:
                    logger.warning(f"Organiser {organiser_id} no longer connected — could not add event to their calendar")

                update_invite(token, "accepted")

                # Notify organiser
                invitee_name = invite.get("invitee_name") or "Someone"
                ev_title = ev.get("title", "your event")
                ev_date  = ev.get("date", "")
                ev_time  = f" at {ev['start_time']}" if ev.get("start_time") else ""
                if telegram_app:
                    import asyncio
                    asyncio.run_coroutine_threadsafe(
                        telegram_app.bot.send_message(
                            chat_id=organiser_id,
                            text=f"🎉 *{invitee_name} accepted your invite!*\n\n"
                                 f"• *{ev_title}* — {ev_date}{ev_time}\n\n"
                                 f"The event has been added to both calendars.",
                            parse_mode="Markdown"),
                        telegram_app.update_queue._loop
                            if hasattr(telegram_app.update_queue, "_loop")
                            else asyncio.new_event_loop()
                    )
                self._html(200, _simple_page("🎉", "You're in!",
                    f"<strong>{ev.get('title','The event')}</strong> has been added to your Google Calendar.<br><br>The organiser has been notified."))
            except Exception as e:
                logger.exception("Invite callback error")
                self._html(500, _simple_page("❌", "Something went wrong", str(e)))
            return

        # ── Invite: counter-propose ───────────────────────────────────────────
        if parsed.path.startswith("/invite/counter/"):
            token = parsed.path.split("/invite/counter/")[-1].strip("/")

            # Validate token format (only allow URL-safe base64 chars)
            if not re.match(r'^[A-Za-z0-9_-]{10,40}$', token):
                self._html(400, _simple_page("❌", "Invalid request", "Bad token format."))
                return

            params = parse_qs(parsed.query)
            counter_date_raw = params.get("date", [None])[0]
            counter_time_raw = params.get("time", [None])[0]
            name_raw         = params.get("name", ["Someone"])[0]

            # Validate date format: YYYY-MM-DD
            if not counter_date_raw or not re.match(r'^\d{4}-\d{2}-\d{2}$', counter_date_raw):
                self._html(400, _simple_page("❌", "Invalid date", "Please use YYYY-MM-DD format."))
                return

            # Validate time format: HH:MM
            if not counter_time_raw or not re.match(r'^\d{2}:\d{2}$', counter_time_raw):
                self._html(400, _simple_page("❌", "Invalid time", "Please use HH:MM format."))
                return

            # Sanitise name: strip HTML, limit length, allow only safe chars
            name = re.sub(r'[<>&]', '', name_raw)[:60].strip() or "Someone"
            counter_date = counter_date_raw
            counter_time = counter_time_raw
            invite = get_invite(token)
            valid, reason = is_invite_valid(invite)
            if not valid:
                self._html(404, _simple_page("❌", "Invite not available", reason))
                return
            update_invite(token, "countered",
                counter_time=counter_time, counter_date=counter_date, invitee_name=name)
            ev = json.loads(invite["event_data"])
            organiser_id = invite["organiser_telegram_id"]
            if telegram_app:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    telegram_app.bot.send_message(
                        chat_id=organiser_id,
                        text=f"💬 *{name} suggested a different time*\n\n"
                             f"• Original: *{ev.get('title')}* — {ev.get('date','')} {ev.get('start_time','')}\n"
                             f"• Suggested: *{counter_date}* at *{counter_time}*\n\n"
                             f"Reply to this message or update the event manually.",
                        parse_mode="Markdown"),
                    telegram_app.update_queue._loop
                        if hasattr(telegram_app.update_queue, "_loop")
                        else asyncio.new_event_loop()
                )
            self._html(200, _simple_page("💬", "Counter proposal sent!",
                f"Your suggestion ({counter_date} at {counter_time}) has been sent to the organiser."))
            return

        # ── Invite: decline ───────────────────────────────────────────────────
        if parsed.path.startswith("/invite/decline/"):
            token = parsed.path.split("/invite/decline/")[-1].strip("/")
            invite = get_invite(token)
            valid, reason = is_invite_valid(invite)
            if not valid:
                self._html(404, _simple_page("❌", "Invite not available", reason))
                return
            update_invite(token, "declined")
            ev = json.loads(invite["event_data"])
            organiser_id = invite["organiser_telegram_id"]
            if telegram_app:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    telegram_app.bot.send_message(
                        chat_id=organiser_id,
                        text=f"❌ *Someone declined your invite*\n\n"
                             f"• *{ev.get('title')}* — {ev.get('date','')}\n\n"
                             f"You can send them a new invite or adjust the event.",
                        parse_mode="Markdown"),
                    telegram_app.update_queue._loop
                        if hasattr(telegram_app.update_queue, "_loop")
                        else asyncio.new_event_loop()
                )
            self._html(200, _simple_page("👋", "Declined",
                "You've declined the invite. The organiser has been notified."))
            return

        if parsed.path == "/oauth/callback":
            params = parse_qs(parsed.query)
            state = params.get("state", [None])[0]
            code  = params.get("code",  [None])[0]

            if state and code:
                # Load from Supabase first, fall back to memory
                flow = _load_oauth_flow(state) or pending_flows.pop(state, None)
                if not flow:
                    self._html(200,
                        "<h2>Session expired</h2>"
                        "<p>Go back to Telegram and send /start to get a new login link.</p>")
                    return
                try:
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

    def do_POST(self):
        """Handle Razorpay webhook POST requests."""
        parsed = urlparse(self.path)
        if parsed.path != "/razorpay/webhook":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            sig    = self.headers.get("X-Razorpay-Signature", "")

            if RAZORPAY_BILLING_ENABLED:
                if not verify_webhook_signature(body, sig):
                    logger.warning("Razorpay webhook: invalid signature")
                    self.send_response(400)
                    self.end_headers()
                    return

                event = parse_webhook(body)
                ev_type     = event.get("event", "")
                telegram_id = event.get("telegram_id")
                sub_id      = event.get("subscription_id")
                plan        = event.get("plan", "basic")

                logger.info(f"Razorpay webhook: {ev_type} tid={telegram_id} sub={sub_id}")

                if telegram_id:
                    if ev_type == "subscription.activated":
                        activate_subscription(telegram_id, sub_id, plan)
                        if telegram_app:
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                telegram_app.bot.send_message(
                                    chat_id=telegram_id,
                                    text="🎉 *Subscription activated!*\n\nYou now have full access to CalendarBot. "
                                         "Just send a message to add events to your calendar!",
                                    parse_mode="Markdown"),
                                telegram_app.update_queue._loop
                                    if hasattr(telegram_app.update_queue, "_loop")
                                    else asyncio.new_event_loop()
                            )

                    elif ev_type in ("subscription.cancelled", "subscription.expired",
                                     "subscription.completed"):
                        deactivate_subscription(telegram_id)
                        if telegram_app:
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                telegram_app.bot.send_message(
                                    chat_id=telegram_id,
                                    text="ℹ️ Your CalendarBot subscription has ended. "
                                         "Use /subscribe to reactivate.",
                                    parse_mode="Markdown"),
                                telegram_app.update_queue._loop
                                    if hasattr(telegram_app.update_queue, "_loop")
                                    else asyncio.new_event_loop()
                            )

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        except Exception as e:
            logger.exception(f"Webhook handler error: {e}")
            self.send_response(500)
            self.end_headers()

    def _html(self, code, body):
        # If body is already a full HTML page, send it as-is
        if body.lstrip().startswith("<!DOCTYPE") or body.lstrip().startswith("<html"):
            full = body
        else:
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
    telegram_app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    telegram_app.add_handler(CommandHandler("cancel", cmd_cancel))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    telegram_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))

    logger.info("CalendarBot running — natural language mode")
    telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

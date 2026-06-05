# 📅 CalendarBot

An AI-powered Telegram bot that lets users manage their Google Calendar using natural language, voice messages, photos, and documents. Includes a full SaaS billing layer powered by Razorpay.

---

## Features

- Natural language event management ("Dentist tomorrow at 3pm")
- Voice message, photo, and PDF input
- Google Calendar OAuth integration (per-user, AES-256 encrypted)
- Calendar invites with accept / decline / counter-propose
- SaaS billing with 7-day free trial + Razorpay subscriptions
- Per-user rate limiting (30 messages / 60 seconds)
- Dockerized, deploy-anywhere

---

## Tech Stack

| Layer | Tool |
|---|---|
| Bot | python-telegram-bot 21.6 |
| AI | Groq (LLaMA 3) |
| Calendar | Google Calendar API |
| Database | Supabase (Postgres) |
| Payments | Razorpay |
| Hosting | Render (recommended) |

---

## Setup Guide

### 1. Clone & install

```bash
git clone <your-repo>
cd telegram-calendar-bot
pip install -r requirements.txt
```

---

### 2. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the steps
3. Copy the **bot token** → `TELEGRAM_TOKEN`

Set bot commands so users see a menu:
```
/start - Start the bot
/subscribe - View plans & subscribe
/cancel - Cancel your subscription
```
(BotFather → /mybots → your bot → Edit Bot → Edit Commands)

---

### 3. Set up Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable the **Google Calendar API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Choose **Web application**
6. Add Authorised Redirect URI:
   ```
   https://your-app.onrender.com/oauth/callback
   https://your-app.onrender.com/invite/callback
   ```
7. Download the JSON file → save as `google_credentials.json`
8. Base64-encode it:
   ```bash
   base64 -i google_credentials.json | tr -d '\n'
   ```
9. Paste the output as `GOOGLE_CREDENTIALS_B64`

Also set up the **OAuth consent screen**:
- User type: External
- Add scope: `https://www.googleapis.com/auth/calendar.events`
- Add your test users while in development

---

### 4. Set up Supabase

1. Create a free project at [supabase.com](https://supabase.com)
2. Go to **SQL Editor** and run the contents of `supabase_migration.sql`
3. Copy your **Project URL** → `SUPABASE_URL`
4. Copy your **anon/public key** → `SUPABASE_KEY`

The migration creates:
- `users` table (already existed in your project)
- `subscriptions` table (trial/active/cancelled per user)
- `rate_limits` table (sliding window per user)
- `active_subscribers` view (useful for dashboards)

---

### 5. Set up Razorpay

1. Create an account at [razorpay.com](https://razorpay.com)
2. Go to **Settings → API Keys → Generate Test Key**
3. Copy **Key ID** → `RAZORPAY_KEY_ID`
4. Copy **Key Secret** → `RAZORPAY_KEY_SECRET`

**Create subscription plans:**
1. Dashboard → Subscriptions → Plans → Create Plan
2. Create the **Basic** plan:
   - Plan name: `CalendarBot Basic`
   - Billing cycle: Monthly
   - Amount: `19900` (paise = ₹199)
3. Create the **Pro** plan:
   - Plan name: `CalendarBot Pro`
   - Billing cycle: Monthly
   - Amount: `49900` (paise = ₹499)
4. Copy each plan's Plan ID → `RAZORPAY_PLAN_BASIC` / `RAZORPAY_PLAN_PRO`

**Set up the webhook:**
1. Dashboard → Webhooks → Add New Webhook
2. Webhook URL: `https://your-app.onrender.com/razorpay/webhook`
3. Secret: choose any strong password → `RAZORPAY_WEBHOOK_SECRET`
4. Enable these events:
   - `subscription.activated`
   - `subscription.cancelled`
   - `subscription.expired`
   - `subscription.completed`

---

### 6. Deploy to Render

1. Push your code to a GitHub repo
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Settings:
   - **Environment**: Docker
   - **Port**: 8080
5. Add all environment variables from `.env.example`
6. Deploy
7. Copy your Render URL (e.g. `https://calendarbot-xyz.onrender.com`) → `RENDER_URL`
8. Update your Google OAuth redirect URIs with this URL

> **Note:** Free Render instances sleep after 15 minutes of inactivity. Upgrade to a paid instance (from $7/month) for production, or use the `/ping` endpoint with an uptime monitor like UptimeRobot.

---

### 7. Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Telegram bot token from BotFather |
| `GROQ_API_KEY` | ✅ | Groq API key for AI |
| `SUPABASE_URL` | ✅ | Supabase project URL |
| `SUPABASE_KEY` | ✅ | Supabase anon key |
| `RENDER_URL` | ✅ | Your deployment URL (no trailing slash) |
| `GOOGLE_CREDENTIALS_B64` | ✅ | Base64-encoded Google OAuth JSON |
| `TOKEN_ENCRYPT_KEY` | ✅ | Strong random string for token encryption |
| `RAZORPAY_KEY_ID` | ⚡ | Razorpay key ID (billing optional) |
| `RAZORPAY_KEY_SECRET` | ⚡ | Razorpay key secret |
| `RAZORPAY_PLAN_BASIC` | ⚡ | Razorpay plan ID for Basic tier |
| `RAZORPAY_PLAN_PRO` | ⚡ | Razorpay plan ID for Pro tier |
| `RAZORPAY_WEBHOOK_SECRET` | ⚡ | Razorpay webhook secret |
| `GOOGLE_CALENDAR_ID` | ➖ | Calendar ID (default: `primary`) |
| `TIMEZONE` | ➖ | Timezone (default: `Asia/Kolkata`) |

⚡ = Optional but needed for billing. If Razorpay vars are missing, billing is disabled and all users get full access.

---

## User Flow

```
User sends /start
  → 7-day free trial starts automatically
  → Bot prompts to connect Google Calendar

User connects Google Calendar (OAuth)
  → Can now add/edit/delete events via natural language

During trial (7 days):
  → Full access, trial countdown shown on /start

Trial expires:
  → User sees "Trial expired" on next message
  → Prompted to /subscribe

User sends /subscribe basic
  → Bot sends Razorpay payment link
  → User pays ₹199 via UPI/card/netbanking
  → Razorpay fires webhook → bot activates subscription
  → Bot sends "Subscription activated!" message

User sends /cancel
  → Subscription cancelled at end of billing cycle
  → User retains access until period ends
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/ping` | Health check — returns `OK` |
| GET | `/oauth/callback` | Google OAuth redirect for bot users |
| GET | `/invite/<token>` | Invite landing page |
| GET | `/invite/callback` | Google OAuth for invitees |
| GET | `/invite/counter/<token>` | Counter-propose time |
| GET | `/invite/decline/<token>` | Decline invite |
| GET | `/payment/success` | Post-payment success page |
| POST | `/razorpay/webhook` | Razorpay subscription events |

---

## Pricing

| Plan | Price | Features |
|---|---|---|
| Free trial | ₹0 / 7 days | Full access |
| Basic | ₹199/month | Unlimited events, AI, voice & photo |
| Pro | ₹499/month | Everything + invites, counter-proposals |

---

## Known Limitations & Future Improvements

- **Fixed PBKDF2 salt**: The encryption salt is static (`calendarbot-v1`). Security is maintained by the secret `TOKEN_ENCRYPT_KEY`, but a per-user salt would be stronger.
- **In-memory OAuth state**: `pending_flows` is stored in memory. A server restart mid-OAuth will break the login flow for that user. Moving this to Supabase would eliminate it.
- **No input sanitisation on counter-propose**: The `/invite/counter/<token>` endpoint reads date/time/name from URL query params without strict format validation.
- **Single instance**: The bot runs as a single process. For high load, consider a proper task queue (Celery + Redis).

---

## License

This software is licensed for **personal and commercial use by the original purchaser only**. Reselling, redistributing, or sublicensing this codebase is not permitted.

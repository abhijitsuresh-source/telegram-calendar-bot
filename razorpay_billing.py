"""
razorpay_billing.py — Razorpay subscription & payment integration for CalendarBot
"""

import os
import hmac
import hashlib
import json
import logging
import razorpay

logger = logging.getLogger(__name__)

RAZORPAY_KEY_ID     = os.environ["RAZORPAY_KEY_ID"]
RAZORPAY_KEY_SECRET = os.environ["RAZORPAY_KEY_SECRET"]
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

rz = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ── Plan config ────────────────────────────────────────────────────────────────
# Create these plans once in Razorpay dashboard, then paste the plan IDs here.
# Dashboard → Subscriptions → Plans → Create Plan
PLANS = {
    "basic": {
        "plan_id": os.environ.get("RAZORPAY_PLAN_BASIC", ""),   # e.g. plan_ABC123
        "price":   199,   # ₹199/month
        "label":   "Basic",
        "features": ["Unlimited events", "AI natural language", "Voice & photo input"],
    },
    "pro": {
        "plan_id": os.environ.get("RAZORPAY_PLAN_PRO", ""),     # e.g. plan_XYZ456
        "price":   499,   # ₹499/month
        "label":   "Pro",
        "features": ["Everything in Basic", "Calendar invites", "Counter-proposals", "Priority support"],
    },
}

TRIAL_DAYS = 7


# ── Customer management ────────────────────────────────────────────────────────

def create_or_get_customer(telegram_id: int, name: str = "CalendarBot User", contact: str = None):
    """Create a Razorpay customer for this Telegram user."""
    try:
        customer = rz.customer.create({
            "name": name,
            "notes": {"telegram_id": str(telegram_id)},
            **({"contact": contact} if contact else {}),
        })
        return customer["id"]
    except Exception as e:
        logger.error(f"Error creating Razorpay customer: {e}")
        return None


# ── Subscription management ───────────────────────────────────────────────────

def create_subscription(plan: str, customer_id: str, telegram_id: int):
    """
    Create a Razorpay subscription for the given plan.
    Returns (subscription_id, short_url) or (None, None) on failure.
    """
    plan_config = PLANS.get(plan)
    if not plan_config or not plan_config["plan_id"]:
        logger.error(f"Plan '{plan}' not configured or missing plan_id")
        return None, None

    try:
        sub = rz.subscription.create({
            "plan_id":       plan_config["plan_id"],
            "customer_notify": 1,
            "quantity":      1,
            "total_count":   120,   # max 10 years of monthly billing
            "notes": {
                "telegram_id": str(telegram_id),
                "plan":        plan,
            },
        })
        # Generate a payment link for the subscription
        link = rz.payment_link.create({
            "amount":   plan_config["price"] * 100,  # paise
            "currency": "INR",
            "description": f"CalendarBot {plan_config['label']} — ₹{plan_config['price']}/month",
            "subscription_id": sub["id"],
            "customer": {"name": "CalendarBot User"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"telegram_id": str(telegram_id), "plan": plan},
            "callback_url": f"{os.environ.get('RENDER_URL', '')}/payment/success",
            "callback_method": "get",
        })
        return sub["id"], link["short_url"]
    except Exception as e:
        logger.error(f"Error creating Razorpay subscription: {e}")
        return None, None


def cancel_subscription(razorpay_sub_id: str):
    """Cancel at end of current billing cycle."""
    try:
        rz.subscription.cancel(razorpay_sub_id, {"cancel_at_cycle_end": 1})
        return True
    except Exception as e:
        logger.error(f"Error cancelling subscription {razorpay_sub_id}: {e}")
        return False


# ── Webhook verification ──────────────────────────────────────────────────────

def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Verify Razorpay webhook signature using HMAC-SHA256."""
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set — skipping verification")
        return True
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_webhook(body: bytes) -> dict:
    """Parse webhook payload and return relevant fields."""
    try:
        data = json.loads(body)
        event    = data.get("event", "")
        payload  = data.get("payload", {})
        sub_data = payload.get("subscription", {}).get("entity", {})
        pay_data = payload.get("payment", {}).get("entity", {})

        telegram_id = None
        notes = sub_data.get("notes") or pay_data.get("notes") or {}
        if isinstance(notes, dict):
            telegram_id = notes.get("telegram_id")

        return {
            "event":            event,
            "subscription_id":  sub_data.get("id"),
            "payment_id":       pay_data.get("id"),
            "telegram_id":      int(telegram_id) if telegram_id else None,
            "plan":             (sub_data.get("notes") or {}).get("plan", "basic"),
            "status":           sub_data.get("status"),
        }
    except Exception as e:
        logger.error(f"Webhook parse error: {e}")
        return {}

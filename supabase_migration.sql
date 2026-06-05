-- ============================================================
-- CalendarBot SaaS — Supabase Migration
-- Run this in your Supabase SQL editor
-- ============================================================

-- 1. Subscriptions table
CREATE TABLE IF NOT EXISTS subscriptions (
    id                  BIGSERIAL PRIMARY KEY,
    telegram_id         BIGINT UNIQUE NOT NULL,
    razorpay_sub_id     TEXT,              -- Razorpay subscription ID (e.g. sub_ABC123)
    razorpay_customer_id TEXT,             -- Razorpay customer ID
    plan                TEXT DEFAULT 'free',  -- 'trial', 'basic', 'pro', 'free'
    status              TEXT DEFAULT 'trial', -- 'trial', 'active', 'cancelled', 'expired'
    trial_started_at    TIMESTAMPTZ DEFAULT NOW(),
    trial_ends_at       TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '7 days'),
    subscribed_at       TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Rate limiting table
CREATE TABLE IF NOT EXISTS rate_limits (
    telegram_id         BIGINT PRIMARY KEY,
    message_count       INT DEFAULT 0,
    window_start        TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_subscriptions_telegram_id ON subscriptions(telegram_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_razorpay_sub_id ON subscriptions(razorpay_sub_id);

-- 4. Helper function: auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER subscriptions_updated_at
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- 5. View: active subscribers (useful for dashboards)
CREATE OR REPLACE VIEW active_subscribers AS
SELECT
    s.telegram_id,
    s.plan,
    s.status,
    s.subscribed_at,
    s.trial_ends_at,
    u.connected AS google_connected
FROM subscriptions s
LEFT JOIN users u ON u.telegram_id = s.telegram_id
WHERE s.status IN ('active', 'trial')
  AND (s.status = 'active' OR s.trial_ends_at > NOW());

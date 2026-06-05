-- ============================================================
-- CalendarBot SaaS — Supabase Migration (with RLS)
-- Run this in your Supabase SQL editor
-- ============================================================

-- 1. Subscriptions table
CREATE TABLE IF NOT EXISTS subscriptions (
    id                   BIGSERIAL PRIMARY KEY,
    telegram_id          BIGINT UNIQUE NOT NULL,
    razorpay_sub_id      TEXT,
    razorpay_customer_id TEXT,
    plan                 TEXT DEFAULT 'free',
    status               TEXT DEFAULT 'trial',
    trial_started_at     TIMESTAMPTZ DEFAULT NOW(),
    trial_ends_at        TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '7 days'),
    subscribed_at        TIMESTAMPTZ,
    cancelled_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Rate limiting table
CREATE TABLE IF NOT EXISTS rate_limits (
    telegram_id   BIGINT PRIMARY KEY,
    message_count INT DEFAULT 0,
    window_start  TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_subscriptions_telegram_id    ON subscriptions(telegram_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status         ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_razorpay_sub_id ON subscriptions(razorpay_sub_id);

-- 4. Auto-update updated_at on subscriptions
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS subscriptions_updated_at ON subscriptions;
CREATE TRIGGER subscriptions_updated_at
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- 5. Active subscribers view
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

-- ============================================================
-- RLS — Row Level Security
-- Your bot uses the service_role key (set in SUPABASE_KEY),
-- which bypasses RLS entirely, so the bot always works.
-- RLS only affects connections using the anon key —
-- meaning no one can read your data via the public API.
-- ============================================================

-- Enable RLS on both tables
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_limits   ENABLE ROW LEVEL SECURITY;

-- Drop policies if they already exist (safe to re-run)
DROP POLICY IF EXISTS "No public access to subscriptions" ON subscriptions;
DROP POLICY IF EXISTS "No public access to rate_limits"   ON rate_limits;

-- Block all access via the anon/public key
-- (your bot uses service_role which bypasses this)
CREATE POLICY "No public access to subscriptions"
    ON subscriptions
    FOR ALL
    TO anon
    USING (false);

CREATE POLICY "No public access to rate_limits"
    ON rate_limits
    FOR ALL
    TO anon
    USING (false);

-- ============================================================
-- IMPORTANT: Make sure SUPABASE_KEY in your Render environment
-- is your SERVICE ROLE key, not the anon key.
-- Dashboard → Settings → API → service_role (secret)
-- ============================================================

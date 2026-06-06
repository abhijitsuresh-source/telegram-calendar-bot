-- ============================================================
-- CalendarBot SaaS — Supabase Migration (with RLS)
-- Run this ONCE in your Supabase SQL editor
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

-- 3. OAuth flows table (persists OAuth state across server restarts)
CREATE TABLE IF NOT EXISTS oauth_flows (
    key        TEXT PRIMARY KEY,
    flow_data  TEXT NOT NULL,       -- encrypted serialised OAuth flow
    is_invite  BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Auto-expire flows after 10 minutes (cleanup old ones)
CREATE INDEX IF NOT EXISTS idx_oauth_flows_created_at ON oauth_flows(created_at);

-- 4. Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_subscriptions_telegram_id     ON subscriptions(telegram_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status          ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_razorpay_sub_id ON subscriptions(razorpay_sub_id);

-- 5. Auto-update updated_at on subscriptions
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

-- 6. Active subscribers view
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
-- RLS — Lock down all tables
-- Your bot uses the service_role key which bypasses RLS.
-- The anon key (public) is blocked from everything.
-- ============================================================

-- Enable RLS on new tables
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_limits   ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_flows   ENABLE ROW LEVEL SECURITY;

-- Enable RLS on existing tables
ALTER TABLE users   ENABLE ROW LEVEL SECURITY;
ALTER TABLE invites ENABLE ROW LEVEL SECURITY;

-- Drop any existing policies (safe to re-run)
DROP POLICY IF EXISTS "No public access to oauth_flows"    ON oauth_flows;
DROP POLICY IF EXISTS "No public access to subscriptions" ON subscriptions;
DROP POLICY IF EXISTS "No public access to rate_limits"   ON rate_limits;
DROP POLICY IF EXISTS "No public access to users"         ON users;
DROP POLICY IF EXISTS "No public access to invites"       ON invites;

-- Block anon access to all tables
CREATE POLICY "No public access to oauth_flows"
    ON oauth_flows FOR ALL TO anon USING (false);

CREATE POLICY "No public access to subscriptions"
    ON subscriptions FOR ALL TO anon USING (false);

CREATE POLICY "No public access to rate_limits"
    ON rate_limits FOR ALL TO anon USING (false);

CREATE POLICY "No public access to users"
    ON users FOR ALL TO anon USING (false);

CREATE POLICY "No public access to invites"
    ON invites FOR ALL TO anon USING (false);

-- Lock down the active_subscribers view
REVOKE SELECT ON active_subscribers FROM anon;
REVOKE SELECT ON active_subscribers FROM public;
GRANT  SELECT ON active_subscribers TO service_role;

-- ============================================================
-- Done! All tables are locked. Only your bot (service_role)
-- can read and write data.
-- ============================================================

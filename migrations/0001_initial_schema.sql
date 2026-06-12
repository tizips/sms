-- Initial Postgres schema for the SMS platform.
--
-- This migration creates the tables used by Admin and the PVE Radio Agent:
-- inbound SMS records, outbound queue rows, send strategies, send plans,
-- key/value application state, and dispatch jobs claimed by PVE.
-- Applied versions are tracked in schema_migrations by bin/db-migrate and
-- admin/core.py startup migration logic.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inbound_sms (
  id BIGSERIAL PRIMARY KEY,
  received_at TEXT NOT NULL,
  sender TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  raw_ids TEXT NOT NULL DEFAULT '',
  phone_id TEXT NOT NULL DEFAULT '',
  sms_messages INTEGER NOT NULL DEFAULT 0,
  decoded_parts INTEGER NOT NULL DEFAULT 0,
  forwarded INTEGER NOT NULL DEFAULT 0,
  forward_error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_inbound_received_at ON inbound_sms(received_at);
CREATE INDEX IF NOT EXISTS idx_inbound_sms_sender ON inbound_sms(sender);

CREATE TABLE IF NOT EXISTS outbound_sms (
  id BIGSERIAL PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  destination TEXT NOT NULL,
  text TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  next_attempt_at TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_retries INTEGER NOT NULL DEFAULT 0,
  retry_interval_seconds INTEGER NOT NULL DEFAULT 600,
  submitted_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  command_output TEXT NOT NULL DEFAULT '',
  final_notified INTEGER NOT NULL DEFAULT 0,
  send_type TEXT NOT NULL DEFAULT 'normal',
  strategy_id BIGINT,
  strategy_name TEXT NOT NULL DEFAULT '',
  plan_id BIGINT,
  send_interval_minutes INTEGER NOT NULL DEFAULT 1,
  retry_interval_minutes INTEGER NOT NULL DEFAULT 10,
  command_timeout_seconds INTEGER NOT NULL DEFAULT 45,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  cancel_requested_at TEXT,
  dispatch_job_id BIGINT
);

CREATE INDEX IF NOT EXISTS idx_outbound_status_due ON outbound_sms(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS send_strategies (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  send_interval_minutes INTEGER NOT NULL DEFAULT 1,
  retry_interval_minutes INTEGER NOT NULL DEFAULT 10,
  max_retries INTEGER NOT NULL DEFAULT 1,
  command_timeout_seconds INTEGER NOT NULL DEFAULT 45,
  active INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS send_plans (
  id BIGSERIAL PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  destination TEXT NOT NULL,
  country_code TEXT NOT NULL DEFAULT '',
  phone_number TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  strategy_id BIGINT NOT NULL,
  strategy_name TEXT NOT NULL,
  outbound_id BIGINT,
  active INTEGER NOT NULL DEFAULT 1,
  archived INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_send_plans_outbound ON send_plans(outbound_id);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_dispatch_jobs (
  id BIGSERIAL PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  outbound_id BIGINT NOT NULL REFERENCES outbound_sms(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL,
  status TEXT NOT NULL,
  destination TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_until TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  checked_outbound_status TEXT NOT NULL DEFAULT '',
  checked_at TEXT,
  result_status TEXT NOT NULL DEFAULT '',
  result_message TEXT NOT NULL DEFAULT '',
  submitted_at TEXT,
  completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dispatch_status ON sms_dispatch_jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_dispatch_outbound ON sms_dispatch_jobs(outbound_id);

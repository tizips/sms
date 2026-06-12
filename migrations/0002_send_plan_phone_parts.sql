-- Add normalized phone-number fields to send plans.
--
-- Existing deployments may already have send_plans from 0001. These columns
-- let Admin store the country code and local phone number separately while
-- preserving the combined destination used for actual SMS dispatch.

ALTER TABLE send_plans
  ADD COLUMN IF NOT EXISTS country_code TEXT NOT NULL DEFAULT '';

ALTER TABLE send_plans
  ADD COLUMN IF NOT EXISTS phone_number TEXT NOT NULL DEFAULT '';

# SMS Admin

Lightweight SMS gateway admin console for `/htdocs/sms`.

## Features

- Password-only login.
- Change-password page with current-password verification.
- Dashboard for inbound/outbound counts.
- Dashboard SIM status from `gammu-smsd` phone state, including signal level.
  It refreshes at most once per minute and does not probe the AT port while
  `gammu-smsd` is using it.
- Inbound SMS list from `/htdocs/sms/data/sms.sqlite`.
- Outbound SMS queue with conservative retry behavior.
- Send form with cost confirmation.
- Multiple send strategies:
  - send interval in minutes
  - retry interval in minutes
  - max retries
  - command timeout
  - non-default strategies can be deleted without changing historical outbound snapshots
- Send plans linked to a strategy. Planned items appear in the outbound list as
  `计划发送` with their plan ID; ad hoc items appear as `普通发送`.
- Planned sends use the plan's own interval history. The first send is eligible
  at the plan time; later sends for the same plan check only that plan's last
  successful submission.
- Final failure or ambiguous status notification through the existing 163 SMTP configuration.
- Unicode SMS text is submitted with `gammu-smsd-inject -unicode` and a UTF-8
  process locale to avoid Chinese text corruption.

## Cost Safety

The worker only retries when `gammu-smsd-inject` exits with a clear non-zero
failure before a submission is accepted. If the command times out or the state
is ambiguous, the item is marked `ambiguous`, no retry is attempted, and an
email notification is sent.

This intentionally favors manual review over duplicate SMS charges.

## Password Changes

The password change page rewrites `/htdocs/sms/conf/admin.env` with a new
PBKDF2 hash and rotates `SESSION_SECRET`. Existing login cookies become
invalid immediately, so the operator must sign in again with the new password.

@/Users/orange/.codex/RTK.md

## Commit Policy

When committing, split changed files into separate commits by feature or
purpose instead of bundling unrelated changes together.

## SQLite Data Safety

The SQLite files copied from the SMS VM under `/htdocs/sms/data` must be
schema-only before they are saved in this repository or uploaded to git.

- Do not commit live VM rows from `sms.sqlite` or `gammu-smsd.sqlite`.
- Keep only table, index, and trigger structure in local `data/*.sqlite`
  snapshots.
- Before adding these files to git, verify every user table and
  `sqlite_sequence` has `COUNT(*) = 0`.
- If refreshing from the VM, create schema-only clone databases first, then
  copy those clones into `data/`; do not copy production data and clean it
  afterward.

@/Users/orange/.codex/RTK.md

## Commit Policy

When committing, split changed files into separate commits by feature or
purpose instead of bundling unrelated changes together.

## Admin Deployment

Deploy SMS Admin as a prebuilt Docker image. Do not build the admin image on
the runtime server.

- Build locally for the server platform: `rtk docker build --platform linux/amd64 -t sms/admin:latest .`.
- Verify the local image before upload, at minimum with `py_compile` for the
  admin Python modules and a focused check for the behavior being deployed.
- Export and compress the image locally, then upload it to
  `tizips@192.168.6.7:/htdocs/docker/sms/`.
- On the server, load the uploaded archive with `sudo docker load`; the running
  image tag must be `sms/admin:latest`.
- The server compose file may vary by host. It only needs to run the loaded
  `sms/admin:latest` image with the required environment, ports, and network for
  that host.
- Bind mounts are conditional:
  - Keep `conf` mounted when `admin.env`, SMTP config, or password changes from
    the UI must persist across container replacement.
  - Mount `logs` only when file logs need to survive container replacement or be
    inspected directly from the host.
- Back up the server `docker-compose.yml` before changing it.
- Restart with `sudo docker compose up -d`, then verify `sms-admin` is
  `running healthy` and the login/dashboard responds over HTTP.
- Do not save passwords, `.env` contents, live database rows, or other runtime
  secrets in this repository.

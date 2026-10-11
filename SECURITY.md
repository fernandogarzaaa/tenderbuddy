# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | yes |
| < 1.0 (beta) | no |

Security fixes go to the latest 1.x release and the `main` branch.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Report privately by email to **fernandogarzaaa@gmail.com** (or through GitHub private
vulnerability reporting in the Security tab, if enabled). Include a description,
steps to reproduce, and the affected version or commit if you know it.

You can expect an acknowledgement within a few days. Please allow a reasonable window to
investigate and release a fix before any public disclosure.

## Deployment hardening

- Run with `APP_ENV=production`: the app refuses to start with the default `SECRET_KEY`
  or `ADMIN_PASSWORD`, or with demo data seeding enabled.
- Use a random `SECRET_KEY` of 32+ characters and keep `.env` out of version control.
- Serve over HTTPS behind a reverse proxy and keep `COOKIE_SECURE=true`.
- Store API keys (`SAM_API_KEY`, `LLM_API_KEY`) only in the environment or a secret manager.
- Install from the hashed `requirements.lock` for reproducible, tamper-evident dependencies.

## What is in place

bcrypt password hashing, signed JWT session/API tokens (PyJWT), HttpOnly SameSite cookies,
CSRF tokens on every form, login rate limiting, security headers (CSP, X-Frame-Options,
X-Content-Type-Options, Referrer-Policy, HSTS when secure), upload size limits and path
checks on document downloads. Dependencies are monitored by Dependabot.

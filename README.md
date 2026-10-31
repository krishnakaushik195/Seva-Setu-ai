# Sewa Setu — Old Age Pension Portal

Department of Social Welfare, Government of Purvanchal. Maintained in-house.

## Running

```
cp .env.example .env      # then set SECRET_KEY and ADMIN_PASSWORD
docker compose up --build
```

The portal refuses to start without a random `SECRET_KEY` (the key that used to sit in
`config/app.ini` shipped with the code, so anyone could have forged an officer session).

- Portal: http://localhost:8000
- Status portal: http://localhost:8000/status (mobile number + password; the
  password is the applicant's date of birth as DDMMYYYY)
- Departmental login: http://localhost:8000/admin (user `admin`, password from
  `ADMIN_PASSWORD`)
- SMS gateway console (simulated telecom): http://localhost:8000/__gateway/ —
  OTPs and notifications appear here once "delivered" (1–4 s carrier latency).
  The gateway's inbox is also readable as JSON at
  `/__gateway/api/messages?to=<mobile>`.
- Database: Postgres, loaded from `seed/seed.sql` on first boot (~100k applications).

## Deploying on a server

Set `PUBLIC_BASE_URL` to the address people will use (for example
`https://sewasetu.example.in`). It is stamped into sign-in tokens and the discovery documents
described below, so a wrong value means other applications cannot find the sign-in page. Put
the portal behind HTTPS; some applications refuse to sign in over plain HTTP.

```
PUBLIC_BASE_URL=https://sewasetu.example.in ADMIN_PASSWORD=pick-something docker compose up --build -d
```

## What the portal does

Citizen flow: mobile → OTP → personal details → address → bank details →
age-proof upload → preview and declaration → application number. A nightly
job (`scripts/deemed_approval.py`, via cron in the `scheduler` container)
deems approved any application still pending after the 15-day statutory SLA.

Rules enforced at submission, in `validate_application()` in `app/app.py`:
applicant is 60 years of age or above; required fields and an age-proof
document are present; the scheme window is open; and the mobile number holds
no other active (pending / approved / deemed-approved) application. A pending
application can be withdrawn from the status portal, after which the same
mobile may apply again.

Departmental users see a dashboard, list and filter applications, approve or
reject pending ones, and see the audit trail per application.

## Layout

```
app/          Flask application, templates, config, nightly job
seed/         seed.sql — production data snapshot
smsgw/        simulated SMS gateway
```

## Letting other applications act for a citizen or an officer

Sewa Setu is also an **OAuth 2.1 authorization server**. A third-party application can act
for a citizen (scope `citizen`) or for a block officer (scope `officer`) once that person
signs in on the portal and approves it. Citizens sign in by mobile and OTP, officers with the
departmental login. The application never handles the OTP or the password.

- Discovery: `/.well-known/oauth-authorization-server` (RFC 8414)
- Registration: `/oauth/register` (RFC 7591), or a client ID metadata document URL as `client_id`
- Authorization: `/oauth/authorize` (authorization code with PKCE S256; the `resource` the
  application asks for becomes the token's audience, RFC 8707)
- Tokens: `/oauth/token`. Access tokens are RS256 JWTs. A sign-in is a **session with an
  absolute end**: 2 hours for a citizen, 8 hours for an officer, counted from the consent.
  Access tokens are short (20 / 30 minutes) and refresh tokens rotate within the session but
  never extend it; after it ends the person signs in again. Portals are used on shared phones
  and kiosks.
- Sign-out: `/oauth/revoke` (RFC 7009). People can also end any sign-in from the status
  portal (**Connected applications**) or, for officers, the admin dashboard.
- Keys: `/.well-known/jwks.json`

A service that receives one of these tokens verifies it with the JWKS, checks `iss` is this
portal, checks `aud` is its own canonical URL, and reads `scope`, `role` and `sub`
(`citizen:<mobile>` or `officer:<username>`).

Long-lived **program tokens** for unattended use (role officer) are issued by an
administrator at `/admin/tokens`, and can be revoked there.

## The AI-assistant channel

Two MCP servers (streamable HTTP), separate processes behind the same address:

| Endpoint | For | Tools |
|---|---|---|
| `<PUBLIC_BASE_URL>/mcp/citizen` | a citizen's assistant (scope `citizen`) | `get_scheme_info`, `check_eligibility`, `save_application_details`, `get_application_draft`, `get_age_proof_upload_link`, `upload_age_proof`, `review_application`, `submit_application`, `get_my_applications`, `withdraw_application`, `sign_out` |
| `<PUBLIC_BASE_URL>/mcp/officer` | a block officer's assistant (scope `officer`) | `get_my_queue`, `get_case`, `view_age_proof`, `decide_application`, `get_dashboard`, `search_applications` |

- Each server is an OAuth protected resource of this portal (metadata at
  `/.well-known/oauth-protected-resource/mcp/citizen` and `.../mcp/officer`). A token is accepted only if
  it was issued for that exact URL and role, and only while the sign-in behind it is live: ending a
  sign-in (status portal, admin page, or `sign_out`) stops the assistant at once.
- Applications go through the same `validate_application()` / `create_application()` as the web form,
  plus the same duplicate checks; decisions go through the same `record_decision()` as the admin pages.
  Applying through an assistant is limited to `AGENT_PILOT_BLOCKS`.
- The age proof reaches the draft through a one-time link (`/u/<token>`) the applicant opens on a phone,
  or as a file the assistant actually holds. Officers' assistants see it as an image.
- Named officer accounts: `docker compose exec app python manage_officers.py <username> "<Full Name>" "<Block,Block|*>"`.
  Decisions are recorded against the username, with the reason, tagged as made through an assistant.
- How to connect an assistant: `/assistant`. Evals: `python evals/run_evals.py --label "..."`
  (Azure OpenAI settings in `.env`); results at `/eval-dashboard`.

## Deploying on a VM

1. A Linux VM with Docker, ports 80 and 443 open. Without a domain, `<ip-with-dashes>.sslip.io`
   resolves to the VM and Caddy obtains a Let's Encrypt certificate for it.
2. `cp .env.example .env` and set `SECRET_KEY`, `ADMIN_PASSWORD`, `PUBLIC_BASE_URL=https://<host>` and
   `SITE_ADDRESS=<host>`.
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
4. Create officer accounts with `manage_officers.py` (above).

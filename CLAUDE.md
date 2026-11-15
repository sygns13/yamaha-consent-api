# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The workspace `CLAUDE.md` (one level up) is the main guide: architecture, KIZUNA contract, invariants, env flags and
commands. Read it first. This file only adds what is specific to this repo and to the BackOffice work.

## Sources of truth

- `REQUERIMIENTOS_TECNICOS.md` (v1.4; copy of `../requerimientos/REQUERIMIENTOS_TECNICOS.md`): consents, KIZUNA,
  landing API, Twilio. The three copies (workspace, this repo, landing) must stay identical: edit the workspace one,
  `cp` it here and to the landing, then `cmp`.
- `../requerimientos/BACKOFFICE_DEALERS.spec.md` (v1.2): the BackOffice (`/api/v1/backoffice/*`, new tables, CLI).
  For the BackOffice it wins over v1.4; it must not change any v1.4 contract.
- `../REPORTE_CRITERIOS_BACKOFFICE.md`: which BackOffice phases (B0–B9) are closed and what is still open.

## BackOffice rules (spec section 0.2)

- One phase at a time, in order. A phase is closed only when `uv run ruff check`, `uv run ruff format --check` and
  `uv run pytest` pass, and the report is updated.
- Open items (`PENDIENTE-18`, `PENDIENTE-09`) get only their "mientras tanto" behavior plus a `TODO(PENDIENTE-n)`.
  Resolved ones (19–23) get no `TODO`.
- Every new setting goes in `Settings` (`app/core/config.py`), `.env.example` and `deploy/cloudrun.env.yaml`;
  secrets (`SMTP_PASSWORD`) go in `SECRETS` of `tests/test_deploy_config.py` instead of the YAML.
- Never log passwords, hashes, tokens, cookies, or unmasked personal data (DNI, names, email, phone, IP, RUC,
  `username`). Identify users by `id`. Email addresses in logs go through `mask_email` (`app/utils/masking.py`).
- Reuse the existing utils before writing new ones: `masking.py`, `validation.py`, `normalization.py`,
  `datetimes.py` (`to_kizuna_iso`), `client_ip.py`, `tokens.py`, `http_errors.py`.
- New tests use the `tests/test_bo_*.py` prefix. The existing suite (538 tests on 05/11/2026) must keep passing.

## BackOffice code map (grows phase by phase)

- `app/core/backoffice_security.py`:
  - `check_backoffice_settings` (startup: `APP_ENV=production` with `BACKOFFICE_SESSION_COOKIE_SECURE=false` raises
    `InsecureConfigurationError`, reused from `app/core/security.py`).
  - `BackofficeError(status_code, code, detail, field, extra, headers)` + `backoffice_error_handler` →
    `{"detail", "code", "field"?}`. `ErrorCode` and the Annex A.3 texts live here. `account_locked()` says
    "1 minuto" in the singular (v1.2).
  - Cookie helpers (`__session`, `Path=/api/v1/backoffice`, `SameSite=strict`, `HttpOnly`, `Max-Age`).
  - Dependencies: `require_csrf_header` (router-level, runs before the session and the body; exact value
    `XMLHttpRequest`), `get_current_session` / `CurrentSession` (403 `PASSWORD_CHANGE_REQUIRED` if pending),
    `get_current_session_allowing_password_change` (only `me` and `change-password`), `require_module(ModuleCode)`.
  - The session dependency always ends its own transaction (commit) after validating, refreshing
    `last_activity_at` (at most once per 60 s) or revoking (inactive → `USER_DEACTIVATED`, deleted → `USER_DELETED`).
    Endpoints then start a fresh transaction on the same `AsyncSession`. `SESSION_EXPIRED` clears the cookie.
- `app/core/passwords.py`: Argon2id (`PasswordHasher()` defaults), `verify_password` (bad hash → False),
  `verify_dummy_password` (precomputed hash, unknown users), `needs_rehash`, `policy_violations` (returns the unmet
  Annex A.3 texts in order), `policy_error_detail`, `generate_temporary_password` (`max(20, min_length)` chars, no
  `0 O o 1 l I`, retries until it passes the policy for that user). Policy max is **20** (spec v1.2), so
  `BACKOFFICE_PASSWORD_MIN_LENGTH` is limited to 8–20.
- `app/core/middleware.py::BackofficeHeadersMiddleware`: `Cache-Control: no-store` + `nosniff` on every response under
  the prefix (errors too).
- `app/api/v1/backoffice/`: `__init__.py` is the aggregator (no prefix; CSRF dependency). Each sub-router declares the
  **full** prefix (`f"{BACKOFFICE_PREFIX}/auth"`): in FastAPI 0.162 a nested router's include prefix is not part of
  `route.path`, and `RequestIdMiddleware` logs `route.path`. `auth.py`: login, logout, me, change-password,
  password-policy.
- `app/schemas/backoffice/`: `common.py` (`BackofficeRequest` camelCase + `extra="forbid"`, `BackofficeResponse`,
  `ErrorResponse`), `auth.py` (`Me`, ...), `users.py` (`UserCreateRequest` with the user field validators).
- `app/services/backoffice/`:
  - `audit.py`: `record_audit(db, action, context=AuditContext, ...)` adds the row to the caller's transaction.
    `AuditContext.from_request` (IP via `client_ip`, user agent, `request_id_var`); the CLI uses `AuditContext()`.
  - `sessions.py`: `AuthenticatedSession`, create/find/expire/refresh/revoke, `stale_session_condition` (shared with
    `purge`), `purge_user_sessions` (on every successful login, 30 days).
  - `auth.py`: `login` (section 5.5; failed attempts commit before raising), `logout`, `change_password` (revokes all
    sessions with `PASSWORD_CHANGED` and creates a new one = rotation), `build_me`.
  - `users.py`: `create_user`, `ensure_unique_user_fields`, `duplicate_from_integrity_error`,
    `reset_temporary_password`. **Built ahead of B4** for the CLI; B4 reuses them for `POST /users` and
    `POST /users/{id}/reset-password` (pass `source=SOURCE_API` and the actor).
  - `password_reset.py` (B3): `request_password_reset` (forgot-password; returns the email to send or `None`, always
    the same 202 upstream; hourly limit counted under the user's `FOR UPDATE`), `get_reset_token_status`
    (`USED` > `INVALID` > `EXPIRED`), `reset_password`, `invalidate_pending_reset_tokens` (section 4.3: also on any
    password change), `password_changed_message`. **Lock order everywhere: the user row first, then its reset
    tokens** (reset_password reads the token's `user_id` unlocked, locks the user, then the token with
    `populate_existing`), so forgot/reset/change-password never deadlock.
  - `email_templates.py` (B3): `password_reset_email`, `password_changed_email` → `(subject, text, html)`; Annex A.4
    texts; every dynamic value goes through `html.escape`; brand colors copied from the landing's `global.css`.
  - Audit actor: `NULL` for anonymous actions (unknown-user login failure, reset request, reset completion) and the
    CLI; failed logins of an **existing** user record that user as actor (section 4.3 wording).
- `app/utils/`: `normalization.py` + `normalize_person_name`, `normalize_username`; `validation.py` + `validate_dni`,
  `validate_person_name` (Latin letters incl. accents/Ñ, space, `'`, `’`, `.`, `-`, at least one letter, max 100),
  `validate_username`, `validate_user_email`, `validate_mobile` (Annex A.3 messages); `tokens.py` + `generate_token`.
- `app/integrations/email.py` (B3): `EmailMessageData`, `EmailSender` protocol, `FakeEmailSender` (logs masked
  recipient + subject, never the body), `SmtpEmailSender` (`aiosmtplib.send`; `none` → `start_tls=False`, `starttls` →
  `start_tls=True`, `ssl` → `use_tls=True`; any failure is logged as ERROR with the exception type only and never
  raised), `get_email_sender` (dependency; tests override it with `RecordingEmailSender`), `check_email_settings`
  (`EmailConfigurationError`, called in `app/main.py`). Emails are sent from `BackgroundTasks` after the commit.
  `minutes_text()` (in `backoffice_security.py`) writes "1 minuto" in the singular.
- `app/cli/backoffice.py`: `create-admin`, `reset-password`, `purge` (argparse with Spanish errors; exit 1).
  `run(argv, session_factory=, settings=, stdout=, stderr=)` is what the tests call. It does not import `app.main`.
  `purge` detects SQLSTATE `42501` (no DELETE on `audit_logs`) and rolls back everything.
- `app/core/config.py`: BackOffice and SMTP settings (spec section 9.1). `BACKOFFICE_BASE_URL` loses its trailing
  slash, like `LANDING_BASE_URL`.
- `docker-compose.yml`: `mailpit` service (SMTP 1025, UI 8025); the `api` profile overrides `SMTP_HOST`/`SMTP_PORT`.
- Models (B1): `role.py` (`Role`, `BackofficeModule`, `RoleModule`, `RoleCode`, `ModuleCode`), `user.py`,
  `user_session.py` (`SessionRevokedReason`), `password_reset_token.py`, `dealer.py`, `ubigeo.py`, `audit_log.py`
  (`AuditAction`, `AuditEntityType`). Catalog values are `StrEnum`s stored as varchar (no PostgreSQL enums).
  - Columns follow the spec's section 4.3 tables literally (owner's decision): `ubigeo_*` have no timestamps,
    `audit_logs` has only `occurred_at`, `user_sessions`, `password_reset_tokens` and `role_modules` only `created_at`
    (`CreatedAtMixin`). `roles.description` is `NULL`.
  - Partial unique indexes (`WHERE is_deleted = 0`): `uq_users_active_{username,email,dni}` and
    `uq_dealers_active_code` on `lower(dealer_code)` (declared after the `Dealer` class).
- `SEED_TABLES` (`app/models/__init__.py`): tables filled by migrations (roles, modules, permissions, Ubigeo).
  `tests/conftest.py::truncate_tables` skips them, so tests never lose the seeds.
- Migrations: `56b3050c235a_backoffice_access_dealers` (11 tables + role/module seeds) → `b6b72cd353a6_ubigeo_inei_seed`
  (loads `alembic/data/ubigeo_inei.csv`; its downgrade is `TRUNCATE ... CASCADE`, which also empties `dealers`: owner's
  decision). `alembic check` must report no differences.
- Ubigeo dataset: `scripts/build_ubigeo_csv.py` (dev only, not in the image) downloads the pinned source, checks its
  SHA-256 and the expected counts, and writes the CSV + `alembic/data/UBIGEO_FUENTE.md`. Never edit the CSV by hand
  and never fill in Ubigeo data from memory; a missing district goes in a new migration.
- Tests: `test_bo_schema.py` (seeds, Ubigeo, constraints), `test_bo_migrations.py` (creates and drops its own empty
  database `<db>_bo_migrations` to run upgrade/downgrade), `test_bo_ubigeo_dataset.py` (offline, fake `fetch`),
  `test_bo_auth.py`, `test_bo_passwords.py`, `test_bo_cli.py`, `test_bo_password_reset.py`, `test_bo_email.py`.
  - `api_client` overrides `get_email_sender` with `RecordingEmailSender` (`email_sender` fixture, `.messages`).
    The reset token only exists in the recorded email text (regex on the link).
  - `test_bo_email.py::test_reset_email_reaches_mailpit` (`integration`) sends a real email to Mailpit through
    `forgot-password`, checks it via `GET /api/v1/messages` and deletes only that message.
  - When checking logs for secrets, ignore the `httpx` logger: it is the test client logging the URL it requested.
  - Fixtures (`tests/conftest.py`): `admin_user`, `operator_user`, `make_bo_client()` / `bo_client` (own cookie jar,
    `X-Requested-With` preset). `test_settings` has `backoffice_session_cookie_secure=False` (httpx does not send
    `Secure` cookies to `http://test`). Helpers in `tests/bo_helpers.py` (`create_bo_user`, `login_as`,
    `update_user`, `update_sessions`, `audit_rows`, ...). Time is simulated by updating the DB, never with `sleep`.
  - Routes that need a session/module are tested on a separate `FastAPI()` built in the test (`protected_client`),
    so the real app is never modified.
  - `test_bo_cli.py::test_purge_requires_the_owner` creates a temporary role (`bo_purge_test_*`) with
    `consent_app`-like grants and drops it at the end; it skips if the DB user cannot create roles.

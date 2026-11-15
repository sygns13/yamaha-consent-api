# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The workspace `CLAUDE.md` (one level up) is the main guide: architecture, KIZUNA contract, invariants, env flags and
commands. Read it first. This file only adds what is specific to this repo and to the BackOffice work.

## Sources of truth

- `REQUERIMIENTOS_TECNICOS.md` (v1.4; copy of `../requerimientos/REQUERIMIENTOS_TECNICOS.md`): consents, KIZUNA,
  landing API, Twilio. The three copies (workspace, this repo, landing) must stay identical: edit the workspace one,
  `cp` it here and to the landing, then `cmp`.
- `../requerimientos/BACKOFFICE_DEALERS.spec.md` (v1.1): the BackOffice (`/api/v1/backoffice/*`, new tables, CLI).
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

- `app/core/backoffice_security.py`: `check_backoffice_settings` (startup: `APP_ENV=production` with
  `BACKOFFICE_SESSION_COOKIE_SECURE=false` raises `InsecureConfigurationError`, reused from `app/core/security.py`).
  Phase B2 adds the session, module, forced-password-change and CSRF dependencies here.
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
  database `<db>_bo_migrations` to run upgrade/downgrade), `test_bo_ubigeo_dataset.py` (offline, fake `fetch`).

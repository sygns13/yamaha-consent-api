# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The workspace `CLAUDE.md` (one level up) is the main guide: architecture, KIZUNA contract, invariants, env flags and
commands. Read it first. This file only adds what is specific to this repo and to the BackOffice work.

## Sources of truth

- `REQUERIMIENTOS_TECNICOS.md` (v1.6; copy of `../requerimientos/REQUERIMIENTOS_TECNICOS.md`): consents, KIZUNA,
  landing API, Twilio. The three copies (workspace, this repo, landing) must stay identical: edit the workspace one,
  `cp` it here and to the landing, then `cmp`.
- `../requerimientos/BACKOFFICE_DEALERS.spec.md` (v1.6): the BackOffice (`/api/v1/backoffice/*`, new tables, CLI).
  For the BackOffice it wins over v1.4; it must not change any v1.4 contract.
- `../REPORTE_CRITERIOS_BACKOFFICE.md`: which BackOffice phases (B0–B9) are closed and what is still open.
- `../requerimientos/INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md` (v1.0, 19/11/2026): KIZUNA → API in Production through
  Cloud Run IAM (Google ID token of the service account `kizuna-caller`). It replaces the Apigee inbound of the OAuth2
  spec (Apigee was dropped for cost). Progress: `../REPORTE_CRITERIOS_KIZUNA_CLOUD_RUN_IAM.md`. Branch
  `feat/kizuna-cloud-run-iam`; Apigee stays only in `feat/kizuna-oauth2` (`710ef07`).
- `../requerimientos/INTEGRACION_KIZUNA_OAUTH2.spec.md` (v1.3): still the source for the **outbound** side (API →
  KIZUNA, Entra ID; section 4, phase K4). Progress: `../REPORTE_CRITERIOS_KIZUNA_OAUTH2.md`.
- Deploy: `README.md` "Despliegue manual en Google Cloud" (step 7 = BackOffice), `../GUIA_DESPLIEGUE_PRODUCCION_GCP.md`
  section 19 and `../GUIA_DESPLIEGUE_VPS_PRUEBAS.md` section 13. Since 17/11/2026 the test VPS runs `4214ea3` with head
  `b6b72cd353a6` and `REVOKE UPDATE, DELETE ON audit_logs FROM consent_app` applied.

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
  password-policy, forgot/reset. `profile.py` (B4): `GET/PATCH /profile` (`CurrentSession`). `users.py` (B4): `/roles`
  and `/users*`, all behind `UsersModule = Depends(require_module(USUARIOS))`; path param `{userId}` via
  `Path(alias="userId")`. Mutations commit in the service and then re-read the user (`get_user_detail`) for the body.
  B5: `dealers.py` (`/lookup` and `/export` declared before `/{dealerId}`; export returns a `Response` with the xlsx
  bytes and `Content-Disposition`), `ubigeo.py` (tag `backoffice-catalogs`; path codes are not validated: a
  malformed code is a 404 like an unknown one), `audit.py` (`/audit-logs`, module `AUDITORIA`). Dealers and ubigeo use
  `require_module(DEALERS)` (Admin and Operator).
- `app/schemas/backoffice/`: `common.py` (`BackofficeRequest` camelCase + `extra="forbid"`, `BackofficeResponse`,
  `ErrorResponse`, `error_responses(*codes)` for OpenAPI; lists: `ListQuery` (`page`, `pageSize` validated by hand
  because `Literal[int]` rejects the query string `"20"`, `sortOrder`), `SearchText` (trim, max 100, empty → None),
  generic `Page[T]`, `UserRef`), `auth.py` (`Me`, `CodeName`, ...), `users.py` (`UserCreateRequest` with the user field
  validators; B4: `UserUpdateRequest`, `ProfileUpdateRequest` (absent = unchanged via `model_fields_set`; null names or
  email → 422), `UserListQuery`, `UserSummary`, `UserDetail`, `UserCreatedResponse`, `TemporaryPasswordResponse`).
  List query models are used as `Annotated[Model, Query()]`; unknown query params are ignored. Query base classes:
  `QueryModel` (camelCase aliases) → `PageQuery` (`page`, `pageSize`) → `ListQuery` (+ `sortOrder`). B5: `dealers.py`
  (`DealerCreateRequest`, `DealerUpdateRequest` (no `dealerCode` → extra field = 422), `DealerFilterQuery` (export),
  `DealerListQuery`, `DealerLookupQuery`, `DealerDetail`), `audit.py` (`AuditLogQuery` with `from_` aliased to
  `from`; `to < from` → 422 on `to`; `AuditLogItem`). No `ubigeo.py` schema: ubigeo responses reuse `CodeName`.
- `app/services/backoffice/`:
  - `audit.py`: `record_audit(db, action, context=AuditContext, ...)` adds the row to the caller's transaction.
    `AuditContext.from_request` (IP via `client_ip`, user agent, `request_id_var`); the CLI uses `AuditContext()`.
  - `sessions.py`: `AuthenticatedSession`, create/find/expire/refresh/revoke, `stale_session_condition` (shared with
    `purge`), `purge_user_sessions` (on every successful login, 30 days).
  - `auth.py`: `login` (section 5.5; failed attempts commit before raising), `logout`, `change_password` (revokes all
    sessions with `PASSWORD_CHANGED` and creates a new one = rotation), `build_me`.
  - `users.py`: `create_user`, `ensure_unique_user_fields`, `duplicate_from_integrity_error`, `flush_or_duplicate`,
    `reset_temporary_password` (built in B2 for the CLI). B4 adds `list_users`, `get_user_detail`, `user_refs`
    (`UserRef` by id, deleted users included; reuse it for dealers and audit logs), `update_user`, `activate_user`,
    `deactivate_user`, `unlock_user`, `reset_password_by_admin`, `delete_user`. Owner's decisions (session 3, spec
    4.1): `version` +1 only on `PUT /users` and `PATCH /profile`; `updated_at`/`updated_by` only on a person's action
    on the record (set explicitly: **`User.updated_at` has no `onupdate`**, so login, lockout and own password changes
    do not touch it); no real change → no audit, no version bump; activate/deactivate/unlock are idempotent (audit
    only on change). Lock order: advisory lock `hashtext('backoffice:admins')` first (deactivate, delete, PUT to a
    non-ADMIN role), then the user row `FOR UPDATE`, then sessions/tokens. `LAST_ADMIN` is only reachable through the
    API in a race (the actor is always another active Admin and self-actions fail first); the sequential case is
    tested on the service.
  - `profile.py` (B4): `update_profile` (`currentPassword` checked only if the email changes, before the uniqueness
    check; `updated_by` = the user).
  - `listing.py` (B4): `like_pattern` / `contains_any` (`ILIKE` with `%`, `_`, `\` escaped), `paginate` (total +
    page). Read-only services end their transaction with `commit`, never `rollback`: a rollback expires every loaded
    object even with `expire_on_commit=False`, and a later attribute access would lazy-load (fails in async).
  - `audit.py` also has `AuditContextDep`, `diff_changes(before, after)` → `{"field": {"from", "to"}}` (camelCase)
    and (B5) `list_audit_logs` (`from` at 00:00 and `to` until the end of the day in `KIZUNA_TIMEZONE`; fixed order
    `occurred_at DESC, id DESC`; INET → `str`).
  - `refs.py` (B5): `user_refs(db, ids)` → `{id: UserRef}` (deleted users included) and `full_name`. Lives apart
    because `audit.py` cannot import `users.py` (which imports `audit.py`).
  - `dealers.py` (B5): `get_active_dealer_by_code(session, code)` (**reuse point for the landing**, spec 13 F1;
    `lower(dealer_code) = lower(:code) AND is_deleted = 0`, uses the partial unique index), `DealerRecord` (dealer +
    district + province + department), `get_dealer_detail`, `list_dealers`, `all_dealers` (export), `create_dealer`,
    `update_dealer`, `delete_dealer`. Same rules as users: `version` +1 only on PUT, no real change → no audit;
    checks: 404 → `VERSION_CONFLICT` → `DISTRICT_NOT_FOUND` (create: `DISTRICT_NOT_FOUND` → `DUPLICATE`). A race on
    `uq_dealers_active_code` gives the same 409. Logs only carry `dealer_id` and `dealer_code`.
  - `exports.py` (B5): `build_dealers_workbook` (openpyxl, run with `run_in_threadpool`; every text cell is forced to
    `data_type = "s"` so `=1+1` is never a formula; control chars stripped with `ILLEGAL_CHARACTERS_RE`; datetimes
    in Lima time without tzinfo, `dd/mm/yyyy hh:mm`) and `export_dealers` (writes `DEALERS_EXPORTED` with the
    non-empty filters and `rows`).
  - `ubigeo.py` (B5): departments/provinces/districts ordered by name; unknown → 404.
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
- `app/utils/`: `ruc.py` (B5, Annex B: `normalize_ruc`, `ruc_check_digit`, `is_valid_ruc`, `validate_ruc`);
  `normalization.py` + `normalize_text` (trim, collapse, NFC), `normalize_contact_phone` (B5), `normalize_person_name`,
  `normalize_username`; `validation.py` (B5) + `validate_district_ubigeo`, `validate_contact_phone`,
  `DEALER_CODE_EMPTY` (Annex A.3 text, with a period: `validate_dealer` keeps its v1.4 text, a KIZUNA test asserts
  it), `validate_person_name(name, max_length=100)` (contact name: 150); `validation.py` + `validate_dni`,
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
  `test_bo_auth.py`, `test_bo_passwords.py`, `test_bo_cli.py`, `test_bo_password_reset.py`, `test_bo_email.py`,
  `test_bo_users.py`, `test_bo_profile.py` (B4; `logged_client(make_bo_client, user)` in `bo_helpers.py` returns a
  client with that user's session), `test_bo_dealers.py`, `test_bo_ruc.py`, `test_bo_ubigeo.py`, `test_bo_export.py`,
  `test_bo_audit.py`, `test_bo_postman.py` (the Postman "BackOffice" folder covers every BackOffice operation of the
  OpenAPI and sends the CSRF header), `test_bo_openapi_export.py` (B5; helpers `create_dealer_row`, `make_ruc`).
  - **Collation:** the local `postgres:16.15-alpine3.24` (musl) sorts text by code point (`Ñ` after `Z`); Cloud SQL
    probably will not (unverified). Never assert an order between non-ASCII strings; take Ubigeo counts from `alembic/data/ubigeo_inei.csv`.
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

## KIZUNA OAuth 2.0 outbound code map (`../requerimientos/INTEGRACION_KIZUNA_OAUTH2.spec.md`, phase K1)

- `app/integrations/kizuna_oauth.py`:
  - `KizunaTokenProvider` gets the client-credentials token from KIZUNA's Entra ID (`client_secret_post` or `_basic`,
    RFC 6749 §2.3.1 encoding) and caches it in memory until `expires_in − KIZUNA_OAUTH_REFRESH_MARGIN_SECONDS`.
    Its `asyncio.Lock` means a single request at a time. The cache key is URL + client_id + scope + method; without a
    valid `expires_in` the token is used once.
  - `get_kizuna_token_provider()` is the per-process `lru_cache` singleton. `tests/conftest.py::fresh_kizuna_token_provider`
    clears it around every test, because its lock binds to an event loop.
  - `KizunaTokenSource` binds the provider to the settings of the current send. `KizunaTokenError(retryable,
    http_status)`: network, timeout, 5xx and 429 are retryable; anything else is `FALLIDO`.
  - `last_error` keeps only `error` and `error_description`, prefixed with `Token:`, with the secret redacted.
- `app/integrations/kizuna_client.py`:
  - `KizunaClient.from_settings(..., token_provider=)` picks the mode with `KIZUNA_CALLBACK_AUTH_MODE`: `header` is
    the default and M5 behavior; `oauth2` sends a Bearer.
  - A callback `401` invalidates that token and resends once inside the same attempt (`attempts` +1 only).
  - `KizunaSendResult.error_from_token` marks failures of the token request.
  - `check_kizuna_settings` adds the oauth2 checks: the four values are required, the header must be empty, and with
    `APP_ENV=production` both URLs must be `https`.
- `KizunaNotifier(..., token_provider=None)` falls back to the process singleton, so the token is shared between
  batches and with the startup reprocess.
- Mock (`mocks/kizuna_mock/main.py`):
  - `POST /kizuna/oauth/token` imitates Entra ID. `MOCK_REQUIRE_AUTH`, `MOCK_CLIENT_ID`/`_SECRET`,
    `MOCK_TOKEN_EXPIRES_IN` and `MOCK_TOKEN_FAIL_TIMES` control it.
  - `DELETE /kizuna/oauth/tokens` revokes the issued tokens. `GET /kizuna/received` adds `token_requests` and a
    per-item `authenticated`.
  - Rebuild the container after changing it: `docker compose up -d --build kizuna-mock`.
- Tests: `tests/test_kizuna_oauth.py` (mock as ASGI transport, plus `routed_transport` for odd token responses),
  `tests/test_kizuna_mock.py` (OAuth section) and `tests/test_e2e_local.py::test_accepted_with_oauth2`.
## KIZUNA inbound with Cloud Run IAM (`../requerimientos/INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md`, phase I1)

- Same image, two Cloud Run services: `consent-api` (public: landing, Twilio webhook, BackOffice) and
  `consent-api-kizuna` (private: only KIZUNA; `roles/run.invoker` only for `kizuna-caller`). Cloud Run validates the
  Google ID token before the app; the app never sees the API Key in Production (no `API_KEY` secret there).
- `API_SURFACE` (`all` default / `public` / `kizuna`): `app/api/surface.py::routers_for` / `include_routers` (called in
  `app/main.py`). `/health` is always mounted. Unmounted routes are 404.
- `KIZUNA_INBOUND_AUTH` (`api_key` default / `cloud_run_iam`): `app/core/security.py::authenticated_by_cloud_run` makes
  `require_api_key` skip **only** when it is `cloud_run_iam` **and** `API_SURFACE=kizuna`. `check_security_settings`:
  `cloud_run_iam` outside the `kizuna` surface raises `InsecureConfigurationError`; with it, an INFO reminder; on the
  `public` surface the API Key is not checked. `API_KEY_ENABLED: "true"` stays in the YAML as a fail-safe.
- `scripts/kizuna_env.py` generates `deploy/cloudrun-kizuna.env.yaml` (gitignored) from `deploy/cloudrun.env.yaml`,
  changing only `API_SURFACE`/`KIZUNA_INBOUND_AUTH` (`--env-vars-file` can't be combined with `--update-env-vars`).
- `scripts/export_openapi.py --kizuna --server https://<URL_CONSENT_API_KIZUNA> <out>`: only the `kizuna` tag, `http`
  bearer scheme `GoogleIdToken`, Cloud Run 401/403. The default export (BackOffice snapshot) is unchanged.
- Postman: folder "KIZUNA vía Cloud Run IAM" (`{{kizuna_api_url}}`, Bearer `{{kizuna_id_token}}`; the token is got
  outside Postman with `gcloud auth print-identity-token --impersonate-service-account=... --audiences=...`).
- FastAPI 0.162 keeps included routers as `_IncludedRouter` in `app.routes` (not flattened): list effective paths with
  `app.openapi()["paths"]`.
- Tests: `tests/test_kizuna_cloud_run_iam.py` (surfaces, startup checks incl. subprocess imports of `app.main`, the
  private service against the test DB without `X-API-Key`, env generation, Postman, no Apigee left) and
  `tests/test_kizuna_openapi_export.py`.

## Postman and the frontend OpenAPI snapshot

- `postman/collection.json` is hand-formatted (short objects on one line). Add requests by inserting text, never by
  re-dumping the whole JSON. The "BackOffice" folder (`auth: noauth`, `{{backoffice_base_url}}`) must cover every
  `/api/v1/backoffice` operation (`tests/test_bo_postman.py`).
- `scripts/export_openapi.py` writes the same JSON as `/openapi.json`. After any API change, regenerate
  `../yamaha-consent-backoffice/docs/openapi.json` with it (ask the owner before touching that repo), then run
  `npm run api:types` in the frontend: its `src/lib/api-schema.d.ts` is versioned and `tests/api-types.test.ts`
  fails if it is stale (spec v1.4, section 8.2).

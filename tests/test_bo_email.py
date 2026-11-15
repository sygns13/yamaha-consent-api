"""Correo del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, sección 7 y Anexo A.4; criterios B3.1, B3.2, B3.7 y
B3.8).

B3.8 (`integration`) envía un correo real a Mailpit (docker compose); se salta si Mailpit no responde. Otra URL:
`MAILPIT_URL=http://host:puerto` y `MAILPIT_SMTP_PORT`.
"""

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosmtplib
import httpx
import pytest

from app.core.config import Settings
from app.integrations.email import (
    EmailConfigurationError,
    EmailMessageData,
    FakeEmailSender,
    SmtpEmailSender,
    check_email_settings,
    get_email_sender,
)
from app.models import RoleCode
from app.services.backoffice.email_templates import (
    RESET_SUBJECT,
    password_changed_email,
    password_reset_email,
)
from tests.bo_helpers import BO, Sessions, create_bo_user

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_URL = "postgresql+asyncpg://u:p@localhost:5432/db"
LINK = "https://bo.prueba.test/restablecer-contrasena?t=TOKEN-secreto_123"
MESSAGE = EmailMessageData(
    to="juan.perez@mail.com",
    subject="Asunto de prueba",
    text=f"Enlace: {LINK}",
    html=f"<a href='{LINK}'>x</a>",
)
MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://localhost:8025")
MAILPIT_SMTP_PORT = int(os.environ.get("MAILPIT_SMTP_PORT", "1025"))


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, database_url=DB_URL, **overrides)


SMTP_OK: dict[str, Any] = {
    "email_send_enabled": True,
    "smtp_host": "smtp.gmail.com",
    "email_from_address": "construyendosolucionesbcs@gmail.com",
    "backoffice_base_url": "https://bo.prueba.test",
}


# --- B3.1 FakeEmailSender ---


async def test_fake_sender_logs_without_body_or_link(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.integrations.email"):
        await FakeEmailSender().send(MESSAGE)

    (record,) = caplog.records
    assert record.levelname == "INFO" and record.getMessage().startswith("Correo SIMULADO")
    assert record.to == "j*********@mail.com" and record.subject == "Asunto de prueba"
    logged = caplog.text + str(vars(record))
    for secret in ("TOKEN-secreto_123", "restablecer-contrasena", "juan.perez@", "Enlace"):
        assert secret not in logged


def test_sender_depends_on_email_send_enabled() -> None:
    assert isinstance(get_email_sender(make_settings(email_send_enabled=False)), FakeEmailSender)
    assert isinstance(get_email_sender(make_settings(**SMTP_OK)), SmtpEmailSender)


# --- B3.2 Chequeo al arrancar ---


def test_disabled_email_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.email"):
        check_email_settings(make_settings(email_send_enabled=False))
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert "los correos del BackOffice NO se envían" in record.getMessage()


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"smtp_host": ""}, "faltan SMTP_HOST"),
        ({"smtp_host": "   "}, "faltan SMTP_HOST"),
        ({"email_from_address": ""}, "faltan EMAIL_FROM_ADDRESS"),
        ({"backoffice_base_url": ""}, "faltan BACKOFFICE_BASE_URL"),
        ({"smtp_username": "cuenta@gmail.com"}, "SMTP_USERNAME sin SMTP_PASSWORD"),
        (
            {"smtp_host": "", "email_from_address": "", "backoffice_base_url": ""},
            "faltan SMTP_HOST, EMAIL_FROM_ADDRESS, BACKOFFICE_BASE_URL",
        ),
    ],
)
def test_incomplete_settings_stop_startup(
    caplog: pytest.LogCaptureFixture, overrides: dict[str, str], problem: str
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="app.integrations.email"),
        pytest.raises(EmailConfigurationError, match=problem),
    ):
        check_email_settings(make_settings(**{**SMTP_OK, **overrides}))
    (record,) = caplog.records
    assert record.levelname == "CRITICAL"


@pytest.mark.parametrize(
    "overrides",
    [{}, {"smtp_username": "cuenta@gmail.com", "smtp_password": "clave-de-aplicacion"}],
)
def test_complete_settings_start_without_logs(
    caplog: pytest.LogCaptureFixture, overrides: dict[str, str]
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.email"):
        check_email_settings(make_settings(**{**SMTP_OK, **overrides}))
    assert caplog.records == []


def test_app_does_not_start_with_incomplete_email_settings() -> None:
    """Proceso aparte, como en Cloud Run: la app no carga."""
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        check=False,
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "DATABASE_URL": DB_URL,
            "API_KEY_ENABLED": "false",
            "TWILIO_SEND_ENABLED": "false",
            "KIZUNA_CALLBACK_ENABLED": "false",
            "EMAIL_SEND_ENABLED": "true",
            "SMTP_HOST": "",
            "EMAIL_FROM_ADDRESS": "no-reply@backoffice.local",
            "BACKOFFICE_BASE_URL": "http://localhost:5173",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode != 0
    assert "EmailConfigurationError" in result.stderr
    assert '"severity": "CRITICAL"' in result.stdout


# --- SmtpEmailSender ---


def test_smtp_message_is_multipart_utf8() -> None:
    sender = SmtpEmailSender(make_settings(**SMTP_OK))
    message = EmailMessageData(
        to="ana@correo.test", subject=RESET_SUBJECT, text="Contraseña ñ", html="<p>Ñandú</p>"
    )

    built = message_from_bytes(sender.build_message(message).as_bytes(), policy=policy.default)

    assert str(built["From"]) == "BackOffice Consentimientos Yamaha <construyendosolucionesbcs@gmail.com>"
    assert str(built["To"]) == "ana@correo.test"
    assert str(built["Subject"]) == RESET_SUBJECT  # con tildes y raya, codificado y decodificado sin pérdida
    assert built["Date"] and built["Message-ID"].endswith("@gmail.com>")
    assert built.get_content_type() == "multipart/alternative"
    text_part = built.get_body(preferencelist=("plain",))
    html_part = built.get_body(preferencelist=("html",))
    assert text_part.get_content_charset() == html_part.get_content_charset() == "utf-8"
    assert text_part.get_content().strip() == "Contraseña ñ"
    assert html_part.get_content().strip() == "<p>Ñandú</p>"


@pytest.mark.parametrize(
    ("security", "use_tls", "start_tls"),
    [("starttls", False, True), ("ssl", True, False), ("none", False, False)],
)
async def test_smtp_connection_parameters(
    monkeypatch: pytest.MonkeyPatch, security: str, use_tls: bool, start_tls: bool
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_send(message: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    settings = make_settings(
        **SMTP_OK,
        smtp_port=587,
        smtp_security=security,
        smtp_username="cuenta@gmail.com",
        smtp_password="clave-de-aplicacion",
        smtp_timeout_seconds=7,
    )
    await SmtpEmailSender(settings).send(MESSAGE)

    (kwargs,) = calls
    assert kwargs == {
        "hostname": "smtp.gmail.com",
        "port": 587,
        "username": "cuenta@gmail.com",
        "password": "clave-de-aplicacion",
        "use_tls": use_tls,
        "start_tls": start_tls,
        "timeout": 7,
    }


async def test_smtp_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_send(message: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    await SmtpEmailSender(make_settings(**SMTP_OK, smtp_security="none")).send(MESSAGE)
    assert calls[0]["username"] is None and calls[0]["password"] is None


@pytest.mark.parametrize(
    "error",
    [
        aiosmtplib.SMTPAuthenticationError(535, "auth failed for juan.perez@mail.com"),
        aiosmtplib.SMTPConnectError("no se pudo conectar"),
        aiosmtplib.SMTPTimeoutError("timeout"),
        OSError("red caída"),
        ValueError("remitente mal formado"),
    ],
    ids=["autenticacion", "conexion", "timeout", "red", "remitente"],
)
async def test_smtp_failures_are_logged_and_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    async def failing_send(message: object, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(aiosmtplib, "send", failing_send)
    with caplog.at_level(logging.INFO, logger="app.integrations.email"):
        await SmtpEmailSender(make_settings(**SMTP_OK)).send(MESSAGE)  # no lanza

    (record,) = caplog.records
    assert record.levelname == "ERROR"
    assert record.error_type == type(error).__name__ and record.to == "j*********@mail.com"
    # El mensaje de la excepción (puede traer la dirección) no se loguea, ni el cuerpo.
    logged = caplog.text + str(vars(record))
    for secret in ("juan.perez@", str(error), "TOKEN-secreto_123"):
        assert secret not in logged


async def test_invalid_from_address_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    sender = SmtpEmailSender(make_settings(**{**SMTP_OK, "email_from_address": "no es un correo"}))
    with caplog.at_level(logging.ERROR, logger="app.integrations.email"):
        await sender.send(MESSAGE)
    assert [r.levelname for r in caplog.records] == ["ERROR"]


# --- B3.7 Plantillas ---


def test_reset_template_texts() -> None:
    subject, text, html = password_reset_email(
        first_names="María José", username="mjose", link=LINK, ttl_minutes=30
    )
    assert subject == "Restablece tu contraseña — BackOffice Consentimientos Yamaha"
    assert text == (
        "Hola María José:\n\n"
        "Recibimos una solicitud para restablecer la contraseña de tu usuario mjose en el BackOffice de "
        "Consentimientos de Yamaha.\n\n"
        "Para crear una nueva contraseña, ingresa al siguiente enlace. Vence en 30 minutos y solo puede usarse "
        f"una vez:\n{LINK}\n\n"
        "Si no solicitaste este cambio, ignora este correo: tu contraseña actual seguirá funcionando.\n\n"
        "Este es un mensaje automático; no respondas a este correo.\n"
    )
    assert '<html lang="es">' in html
    assert "<strong>mjose</strong>" in html
    assert f'href="{LINK}"' in html and ">Crear nueva contraseña</a>" in html
    assert html.count(LINK) == 3  # botón, enlace en texto y su href
    assert "Vence en 30 minutos y solo puede usarse una vez" in html
    assert "#3224ff" in html  # color de marca de la landing


def test_changed_template_texts() -> None:
    changed_at = datetime(2026, 11, 15, 15, 35, 59, tzinfo=UTC)
    subject, text, html = password_changed_email(
        first_names="Juan", username="jperez", changed_at=changed_at, zone=ZoneInfo("America/Lima")
    )
    assert subject == "Tu contraseña fue actualizada — BackOffice Consentimientos Yamaha"
    assert text == (
        "Hola Juan:\n\n"
        "La contraseña de tu usuario jperez fue actualizada el 15/11/2026 10:35 (hora de Lima).\n\n"
        "Si no fuiste tú, comunícate de inmediato con el Administrador del BackOffice.\n\n"
        "Este es un mensaje automático; no respondas a este correo.\n"
    )
    assert "<strong>jperez</strong> fue actualizada el 15/11/2026 10:35 (hora de Lima)." in html


def test_templates_escape_html() -> None:
    _, text, html = password_reset_email(
        first_names="<script>alert(1)</script> & Cía", username="a&b", link=LINK + '"&x=<y>', ttl_minutes=30
    )
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Cía" in html
    assert "<strong>a&amp;b</strong>" in html
    assert (
        'href="https://bo.prueba.test/restablecer-contrasena?t=TOKEN-secreto_123&quot;&amp;x=&lt;y&gt;"'
        in html
    )
    assert "Hola <script>alert(1)</script> & Cía:" in text  # el texto plano no se escapa

    _, _, changed = password_changed_email(
        first_names="O'Brien <b>", username="x<y", changed_at=datetime.now(UTC), zone=ZoneInfo("America/Lima")
    )
    assert "O&#x27;Brien &lt;b&gt;" in changed and "<strong>x&lt;y</strong>" in changed


def test_ttl_of_one_minute_is_singular() -> None:
    _, text, html = password_reset_email(first_names="Ana", username="ana1", link=LINK, ttl_minutes=1)
    assert "Vence en 1 minuto y solo" in text and "Vence en 1 minuto y solo" in html


# --- B3.8 Integración con Mailpit ---


async def _mailpit_available() -> bool:
    try:
        async with httpx.AsyncClient(base_url=MAILPIT_URL, timeout=3) as client:
            return (await client.get("/api/v1/info")).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.integration
async def test_reset_email_reaches_mailpit(
    api_client: httpx.AsyncClient, session_factory: Sessions, test_settings: Settings
) -> None:
    if not await _mailpit_available():
        pytest.skip(f"Mailpit no responde en {MAILPIT_URL} (docker compose up -d mailpit)")
    from app.main import app

    test_settings.email_send_enabled = True
    test_settings.smtp_host = "localhost"
    test_settings.smtp_port = MAILPIT_SMTP_PORT
    test_settings.smtp_security = "none"
    test_settings.email_from_address = "no-reply@backoffice.local"
    test_settings.backoffice_base_url = "http://localhost:5173"
    app.dependency_overrides[get_email_sender] = lambda: SmtpEmailSender(test_settings)
    recipient = f"mailpit.{uuid.uuid4().hex[:12]}@backoffice.local"
    await create_bo_user(
        session_factory, username="mailpit.qa", dni="13572468", email=recipient, role_code=RoleCode.ADMIN
    )

    response = await api_client.post(
        f"{BO}/auth/forgot-password",
        json={"identifier": recipient},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 202

    async with httpx.AsyncClient(base_url=MAILPIT_URL, timeout=5) as mailpit:
        found: list[dict[str, Any]] = []
        for _ in range(25):
            listing = (await mailpit.get("/api/v1/messages", params={"limit": 50})).json()
            found = [m for m in listing["messages"] if any(t["Address"] == recipient for t in m["To"])]
            if found:
                break
            await asyncio.sleep(0.2)
        assert len(found) == 1, "el correo no llegó a Mailpit"
        summary = found[0]
        try:
            assert summary["Subject"] == RESET_SUBJECT
            assert summary["From"] == {
                "Name": "BackOffice Consentimientos Yamaha",
                "Address": "no-reply@backoffice.local",
            }
            detail = (await mailpit.get(f"/api/v1/message/{summary['ID']}")).json()
            assert "http://localhost:5173/restablecer-contrasena?t=" in detail["Text"]
            assert "Crear nueva contraseña" in detail["HTML"]
            assert "Hola María José:" in detail["Text"]
        finally:
            await mailpit.request("DELETE", "/api/v1/messages", json={"IDs": [summary["ID"]]})

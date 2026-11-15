"""Correo del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 7).

- EMAIL_SEND_ENABLED=true  → SmtpEmailSender: envío real por SMTP (Mailpit en local; Gmail en el VPS y Producción).
- EMAIL_SEND_ENABLED=false → FakeEmailSender: no envía nada, solo registra en el log.

El envío corre en BackgroundTasks **después del commit**. Un fallo nunca se propaga: se registra como ERROR con el
tipo de la excepción y el destinatario enmascarado. El cuerpo nunca se loguea (el de restablecimiento trae el enlace).
"""

import logging
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Annotated, Protocol

import aiosmtplib
from fastapi import Depends

from app.core.config import Settings, get_settings
from app.utils.masking import mask_email

logger = logging.getLogger(__name__)


class EmailConfigurationError(RuntimeError):
    """EMAIL_SEND_ENABLED=true con la configuración de SMTP incompleta: la aplicación no debe arrancar."""


@dataclass(frozen=True)
class EmailMessageData:
    to: str
    subject: str
    text: str
    html: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessageData) -> None:
        """Nunca lanza: un fallo se registra en el log."""
        ...


class FakeEmailSender:
    """No envía nada: registra el destinatario enmascarado y el asunto. **Nunca** el cuerpo (trae el enlace)."""

    async def send(self, message: EmailMessageData) -> None:
        logger.info(
            "Correo SIMULADO (no se envió ningún correo)",
            extra={"to": mask_email(message.to), "subject": message.subject},
        )


class SmtpEmailSender:
    """Envío por SMTP con aiosmtplib (una conexión por mensaje: son pocos correos)."""

    def __init__(self, settings: Settings) -> None:
        self._hostname = settings.smtp_host
        self._port = settings.smtp_port
        self._username = settings.smtp_username or None
        self._password = settings.smtp_password.get_secret_value() or None
        self._security = settings.smtp_security
        self._timeout = settings.smtp_timeout_seconds
        self._from_name = settings.email_from_name
        self._from_address = settings.email_from_address

    def build_message(self, message: EmailMessageData) -> EmailMessage:
        """Multiparte texto + HTML en UTF-8, remitente "{EMAIL_FROM_NAME}" <{EMAIL_FROM_ADDRESS}>."""
        sender = Address(display_name=self._from_name, addr_spec=self._from_address)
        email = EmailMessage()
        email["From"] = sender
        email["To"] = message.to
        email["Subject"] = message.subject
        email["Date"] = formatdate(localtime=False, usegmt=True)
        email["Message-ID"] = make_msgid(domain=sender.domain or None)
        email.set_content(message.text, charset="utf-8")
        email.add_alternative(message.html, subtype="html", charset="utf-8")
        return email

    async def send(self, message: EmailMessageData) -> None:
        try:
            await aiosmtplib.send(
                self.build_message(message),
                hostname=self._hostname,
                port=self._port,
                username=self._username,
                password=self._password,
                use_tls=self._security == "ssl",
                start_tls=self._security == "starttls",
                timeout=self._timeout,
            )
        # Cualquier fallo (red, TLS, autenticación, timeout, rechazo del servidor, remitente mal formado) se registra y
        # no se propaga (sección 7.2): el correo es un aviso posterior al commit.
        except Exception as exc:  # noqa: BLE001
            # El mensaje de la excepción puede traer la dirección: solo se registra su tipo.
            logger.error(
                "No se pudo enviar el correo del BackOffice",
                extra={"error_type": type(exc).__name__, "to": mask_email(message.to)},
            )
            return
        logger.info("Correo del BackOffice enviado", extra={"to": mask_email(message.to)})


_fake_sender = FakeEmailSender()


def get_email_sender(settings: Annotated[Settings, Depends(get_settings)]) -> EmailSender:
    """Dependencia de FastAPI según EMAIL_SEND_ENABLED. Los tests la reemplazan con RecordingEmailSender."""
    if not settings.email_send_enabled:
        return _fake_sender
    return SmtpEmailSender(settings)


def check_email_settings(settings: Settings) -> None:
    """Se ejecuta al arrancar la app (sección 7.4).

    - EMAIL_SEND_ENABLED=false → log WARNING.
    - EMAIL_SEND_ENABLED=true sin SMTP_HOST, EMAIL_FROM_ADDRESS o BACKOFFICE_BASE_URL, o con SMTP_USERNAME sin
      SMTP_PASSWORD → EmailConfigurationError (log CRITICAL): la app no arranca.
    """
    if not settings.email_send_enabled:
        logger.warning(
            "EMAIL_SEND_ENABLED=false: los correos del BackOffice NO se envían (solo se registran en el log)"
        )
        return

    missing = [
        name
        for name, value in (
            ("SMTP_HOST", settings.smtp_host),
            ("EMAIL_FROM_ADDRESS", settings.email_from_address),
            ("BACKOFFICE_BASE_URL", settings.backoffice_base_url),
        )
        if not value.strip()
    ]
    problems = [f"faltan {', '.join(missing)}"] if missing else []
    if settings.smtp_username.strip() and not settings.smtp_password.get_secret_value().strip():
        problems.append("SMTP_USERNAME sin SMTP_PASSWORD")
    if problems:
        message = (
            "EMAIL_SEND_ENABLED=true pero la configuración del correo del BackOffice está incompleta: "
            f"{'; '.join(problems)}"
        )
        logger.critical(message)
        raise EmailConfigurationError(message)

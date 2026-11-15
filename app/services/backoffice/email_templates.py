"""Plantillas de los correos del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 7.3; textos del Anexo A.4).

Cada función devuelve `(subject, text, html)`. HTML simple con estilos en línea y los colores de marca de la landing
(`--color-brand`, `--color-brand-dark`, `--color-surface` de su `global.css`). Todo valor dinámico pasa por
`html.escape`.
"""

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.core.backoffice_security import minutes_text

BRAND = "#3224ff"
BRAND_DARK = "#2418d6"
SURFACE = "#f2f3f6"
TEXT = "#1f2937"
MUTED = "#6b7280"

PRODUCT = "BackOffice · Consentimientos"
AUTOMATIC_NOTICE = "Este es un mensaje automático; no respondas a este correo."
RESET_SUBJECT = "Restablece tu contraseña — BackOffice Consentimientos Yamaha"
CHANGED_SUBJECT = "Tu contraseña fue actualizada — BackOffice Consentimientos Yamaha"
RESET_BUTTON = "Crear nueva contraseña"


def _paragraph(content: str) -> str:
    return f'<p style="margin:0 0 16px 0;font-size:15px;line-height:22px;color:{TEXT};">{content}</p>'


def _layout(title: str, body: str) -> str:
    return (
        '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head>"
        f'<body style="margin:0;padding:0;background-color:{SURFACE};font-family:Arial,Helvetica,sans-serif;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{SURFACE};">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background-color:#ffffff;border-radius:8px;">'
        f'<tr><td style="background-color:{BRAND};border-radius:8px 8px 0 0;padding:16px 24px;'
        f'color:#ffffff;font-size:16px;font-weight:bold;">{escape(PRODUCT)}</td></tr>'
        f'<tr><td style="padding:24px;">{body}'
        f'<p style="margin:24px 0 0 0;font-size:12px;line-height:18px;color:{MUTED};">{escape(AUTOMATIC_NOTICE)}</p>'
        "</td></tr></table></td></tr></table></body></html>"
    )


def password_reset_email(
    *, first_names: str, username: str, link: str, ttl_minutes: int
) -> tuple[str, str, str]:
    """Correo con el enlace `{BACKOFFICE_BASE_URL}/restablecer-contrasena?t={token}`."""
    ttl = minutes_text(ttl_minutes)
    text = (
        f"Hola {first_names}:\n\n"
        f"Recibimos una solicitud para restablecer la contraseña de tu usuario {username} en el BackOffice de "
        "Consentimientos de Yamaha.\n\n"
        f"Para crear una nueva contraseña, ingresa al siguiente enlace. Vence en {ttl} y solo puede usarse una vez:\n"
        f"{link}\n\n"
        "Si no solicitaste este cambio, ignora este correo: tu contraseña actual seguirá funcionando.\n\n"
        f"{AUTOMATIC_NOTICE}\n"
    )
    safe_link = escape(link, quote=True)
    body = (
        _paragraph(f"Hola {escape(first_names)}:")
        + _paragraph(
            "Recibimos una solicitud para restablecer la contraseña de tu usuario "
            f"<strong>{escape(username)}</strong> en el BackOffice de Consentimientos de Yamaha."
        )
        + _paragraph(
            "Para crear una nueva contraseña, ingresa al siguiente enlace. "
            f"Vence en {escape(ttl)} y solo puede usarse una vez:"
        )
        + '<p style="margin:0 0 16px 0;">'
        f'<a href="{safe_link}" style="display:inline-block;background-color:{BRAND};color:#ffffff;'
        'font-size:15px;font-weight:bold;text-decoration:none;padding:12px 20px;border-radius:6px;">'
        f"{escape(RESET_BUTTON)}</a></p>"
        + f'<p style="margin:0 0 16px 0;font-size:13px;line-height:20px;word-break:break-all;">'
        f'<a href="{safe_link}" style="color:{BRAND_DARK};">{safe_link}</a></p>'
        + _paragraph(
            "Si no solicitaste este cambio, ignora este correo: tu contraseña actual seguirá funcionando."
        )
    )
    return RESET_SUBJECT, text, _layout(RESET_SUBJECT, body)


def password_changed_email(
    *, first_names: str, username: str, changed_at: datetime, zone: ZoneInfo
) -> tuple[str, str, str]:
    """Aviso tras cambiar o restablecer la contraseña. Fecha dd/mm/aaaa HH:mm en la zona de KIZUNA_TIMEZONE (Lima)."""
    when = changed_at.astimezone(zone).strftime("%d/%m/%Y %H:%M")
    text = (
        f"Hola {first_names}:\n\n"
        f"La contraseña de tu usuario {username} fue actualizada el {when} (hora de Lima).\n\n"
        "Si no fuiste tú, comunícate de inmediato con el Administrador del BackOffice.\n\n"
        f"{AUTOMATIC_NOTICE}\n"
    )
    body = (
        _paragraph(f"Hola {escape(first_names)}:")
        + _paragraph(
            f"La contraseña de tu usuario <strong>{escape(username)}</strong> fue actualizada el "
            f"{escape(when)} (hora de Lima)."
        )
        + _paragraph("Si no fuiste tú, comunícate de inmediato con el Administrador del BackOffice.")
    )
    return CHANGED_SUBJECT, text, _layout(CHANGED_SUBJECT, body)

"""CLI de arranque y emergencia del BackOffice (BACKOFFICE_DEALERS.spec.md, secciones 5.11 y 5.13).

    python -m app.cli.backoffice create-admin --dni 12345678 --first-names "Juan" --last-names "Pérez Gómez" \\
        --username jperez --email jperez@dominio.com [--phone 987654321]
    python -m app.cli.backoffice reset-password --username jperez
    python -m app.cli.backoffice purge [--dry-run] [--audit-days 365] [--sessions-days 30]

Va dentro de la imagen: exige acceso directo a la base, que es la barrera. Solo `DATABASE_URL` es obligatoria (el resto
de la configuración usa sus valores por defecto). No importa `app.main`, así que no ejecuta los chequeos de arranque
de Twilio, KIZUNA ni del BackOffice.

La contraseña temporal se imprime una sola vez en stdout; nunca va al log. Los errores van en español por stderr con
código de salida 1.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn, TextIO

from pydantic import ValidationError
from sqlalchemy import Delete, Select, delete, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.backoffice_security import BackofficeError
from app.core.config import Settings, get_settings
from app.core.validation_errors import translate_message
from app.models.audit_log import AuditAction, AuditLog
from app.models.password_reset_token import PasswordResetToken
from app.models.role import Role, RoleCode
from app.models.user import User
from app.models.user_session import UserSession
from app.schemas.backoffice.users import UserCreateRequest
from app.services.backoffice.audit import AuditContext, record_audit
from app.services.backoffice.sessions import SESSION_RETENTION_DAYS, stale_session_condition
from app.services.backoffice.users import SOURCE_CLI, create_user, reset_temporary_password
from app.utils.datetimes import to_kizuna_iso
from app.utils.normalization import normalize_username

AUDIT_RETENTION_DAYS = 365
MIN_AUDIT_DAYS = 90
MIN_SESSIONS_DAYS = 1
INSUFFICIENT_PRIVILEGE = "42501"

PURGE_OWNER_REQUIRED = "La depuración requiere el usuario dueño de la base (consent_owner)."
USER_NOT_FOUND = "No existe un usuario vigente con ese nombre de usuario."

# Argumento de la CLI de cada campo del alta (para los mensajes de validación).
_FIELD_FLAGS = {
    "dni": "--dni",
    "first_names": "--first-names",
    "firstNames": "--first-names",
    "last_names": "--last-names",
    "lastNames": "--last-names",
    "username": "--username",
    "email": "--email",
    "phone": "--phone",
}

# Mensajes de argparse (en inglés) → español.
_ARGPARSE_MESSAGES = [
    (re.compile(r"the following arguments are required: (.+)"), "Faltan argumentos obligatorios: {0}"),
    (re.compile(r"argument (\S+): expected one argument"), "El argumento {0} necesita un valor"),
    (re.compile(r"argument (\S+): invalid int value: .*"), "El argumento {0} debe ser un número entero"),
    (
        re.compile(r"argument (\S+): invalid choice: .*"),
        "Comando no reconocido. Use create-admin, reset-password o purge",
    ),
    (re.compile(r"unrecognized arguments: (.+)"), "Argumentos no reconocidos: {0}"),
]


class CliError(Exception):
    """Error con mensaje en español para stderr (código de salida 1)."""


class _SpanishArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        for pattern, template in _ARGPARSE_MESSAGES:
            match = pattern.fullmatch(message)
            if match:
                raise CliError(template.format(*match.groups()))
        raise CliError(f"Argumentos inválidos: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _SpanishArgumentParser(
        prog="python -m app.cli.backoffice", description="CLI de arranque y emergencia del BackOffice."
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_SpanishArgumentParser)

    create = commands.add_parser("create-admin", help="Crea un Administrador con contraseña temporal")
    create.add_argument("--dni", required=True, help="DNI (8 dígitos)")
    create.add_argument("--first-names", required=True, help="Nombres")
    create.add_argument("--last-names", required=True, help="Apellidos")
    create.add_argument(
        "--username", required=True, help="Usuario (4 a 30 caracteres, empieza con una letra)"
    )
    create.add_argument("--email", required=True, help="Correo (canal de recuperación de contraseña)")
    create.add_argument("--phone", default=None, help="Celular (9 dígitos, opcional)")

    reset = commands.add_parser("reset-password", help="Genera una contraseña temporal para un usuario")
    reset.add_argument("--username", required=True, help="Usuario")

    purge = commands.add_parser("purge", help="Depura bitácora, sesiones y enlaces vencidos (sección 5.13)")
    purge.add_argument(
        "--dry-run", action="store_true", help="Solo cuenta; no borra ni escribe en la bitácora"
    )
    purge.add_argument(
        "--audit-days",
        type=int,
        default=AUDIT_RETENTION_DAYS,
        help=f"Días de bitácora (mínimo {MIN_AUDIT_DAYS})",
    )
    purge.add_argument(
        "--sessions-days",
        type=int,
        default=SESSION_RETENTION_DAYS,
        help=f"Días de sesiones y enlaces (mínimo {MIN_SESSIONS_DAYS})",
    )
    return parser


def _validation_message(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else ""
        lines.append(f"{_FIELD_FLAGS.get(field, field)}: {translate_message(error)}")
    return "\n".join(lines)


def _print_temporary_password(out: TextIO, username: str, role_name: str, password: str) -> None:
    print(f"Usuario: {username} ({role_name})", file=out)
    print(f"Contraseña temporal (se muestra una sola vez): {password}", file=out)
    print("Debe cambiarla en el primer inicio de sesión.", file=out)


async def _role_name(db: AsyncSession, role_code: str) -> str:
    role = await db.get(Role, role_code)
    return role.name if role else role_code


async def create_admin(
    args: argparse.Namespace, sessions: async_sessionmaker[AsyncSession], settings: Settings, out: TextIO
) -> None:
    try:
        data = UserCreateRequest(
            dni=args.dni,
            first_names=args.first_names,
            last_names=args.last_names,
            username=args.username,
            email=args.email,
            phone=args.phone,
            role_code=RoleCode.ADMIN,
        )
    except ValidationError as exc:
        raise CliError(_validation_message(exc)) from None

    async with sessions() as db:
        try:
            user, password = await create_user(
                db, data, settings=settings, context=AuditContext(), source=SOURCE_CLI
            )
        except BackofficeError as exc:
            raise CliError(exc.detail) from None
        role_name = await _role_name(db, user.role_code)
        await db.commit()
    _print_temporary_password(out, user.username, role_name, password)


async def reset_password(
    args: argparse.Namespace, sessions: async_sessionmaker[AsyncSession], settings: Settings, out: TextIO
) -> None:
    async with sessions() as db:
        user = await db.scalar(
            select(User)
            .where(User.username == normalize_username(args.username), User.is_deleted == 0)
            .with_for_update()
        )
        if user is None:
            raise CliError(USER_NOT_FOUND)
        password = await reset_temporary_password(
            db, user, settings=settings, context=AuditContext(), actor_user_id=None, source=SOURCE_CLI
        )
        role_name = await _role_name(db, user.role_code)
        await db.commit()
    _print_temporary_password(out, user.username, role_name, password)


@dataclass(frozen=True)
class PurgeCounts:
    audit_logs: int
    sessions: int
    reset_tokens: int


def _purge_statements(
    audit_cutoff: datetime, sessions_cutoff: datetime
) -> tuple[Delete, Delete, Delete, tuple[Select, Select, Select]]:
    audit_where = AuditLog.occurred_at < audit_cutoff
    session_where = stale_session_condition(sessions_cutoff)
    token_where = PasswordResetToken.created_at < sessions_cutoff
    counts = (
        select(func.count()).select_from(AuditLog).where(audit_where),
        select(func.count()).select_from(UserSession).where(session_where),
        select(func.count()).select_from(PasswordResetToken).where(token_where),
    )
    return (
        delete(AuditLog).where(audit_where),
        delete(UserSession).where(session_where),
        delete(PasswordResetToken).where(token_where),
        counts,
    )


def _is_insufficient_privilege(exc: DBAPIError) -> bool:
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return code == INSUFFICIENT_PRIVILEGE


async def purge(
    args: argparse.Namespace, sessions: async_sessionmaker[AsyncSession], settings: Settings, out: TextIO
) -> PurgeCounts:
    """Sección 5.13. Todo en una transacción: con `consent_app` (sin DELETE sobre audit_logs) revierte todo."""
    if args.audit_days < MIN_AUDIT_DAYS:
        raise CliError(f"--audit-days debe ser {MIN_AUDIT_DAYS} o más.")
    if args.sessions_days < MIN_SESSIONS_DAYS:
        raise CliError(f"--sessions-days debe ser {MIN_SESSIONS_DAYS} o más.")

    now = datetime.now(UTC)
    audit_cutoff = now - timedelta(days=args.audit_days)
    sessions_cutoff = now - timedelta(days=args.sessions_days)
    delete_audit, delete_sessions, delete_tokens, count_queries = _purge_statements(
        audit_cutoff, sessions_cutoff
    )

    async with sessions() as db:
        try:
            if args.dry_run:
                audit_count, session_count, token_count = [
                    await db.scalar(query) or 0 for query in count_queries
                ]
                await db.rollback()
            else:
                session_count = (await db.execute(delete_sessions)).rowcount or 0
                token_count = (await db.execute(delete_tokens)).rowcount or 0
                audit_count = (await db.execute(delete_audit)).rowcount or 0
                record_audit(
                    db,
                    AuditAction.SYSTEM_PURGE,
                    context=AuditContext(),
                    details={
                        "auditLogsDeleted": audit_count,
                        "sessionsDeleted": session_count,
                        "resetTokensDeleted": token_count,
                        "auditCutoff": to_kizuna_iso(audit_cutoff, settings.kizuna_zone),
                        "sessionsCutoff": to_kizuna_iso(sessions_cutoff, settings.kizuna_zone),
                    },
                    occurred_at=now,
                )
                await db.commit()
        except DBAPIError as exc:
            await db.rollback()
            if _is_insufficient_privilege(exc):
                raise CliError(PURGE_OWNER_REQUIRED) from None
            raise

    if args.dry_run:
        print("Simulación (--dry-run): no se borró nada.", file=out)
    print(
        f"Bitácora: {audit_count} · Sesiones: {session_count} · Enlaces de restablecimiento: {token_count}",
        file=out,
    )
    return PurgeCounts(audit_logs=audit_count, sessions=session_count, reset_tokens=token_count)


_COMMANDS = {"create-admin": create_admin, "reset-password": reset_password, "purge": purge}


async def run(
    argv: Sequence[str],
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Ejecuta un comando y devuelve el código de salida (0 bien, 1 error). Los tests pasan su base y su
    configuración."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    engine_to_dispose = None
    try:
        args = build_parser().parse_args(list(argv))
        settings = settings or get_settings()
        if session_factory is None:
            from app.db.session import SessionLocal, engine

            session_factory, engine_to_dispose = SessionLocal, engine
        await _COMMANDS[args.command](args, session_factory, settings, out)
    except CliError as exc:
        print(str(exc), file=err)
        return 1
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()
    return 0


def main() -> None:
    # En Windows, stdout redirigido usa cp1252; los mensajes llevan tildes y Ñ (igual que configure_logging).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()

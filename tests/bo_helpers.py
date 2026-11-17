"""Helpers de los tests del BackOffice (usan la base de pruebas de conftest.py)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.backoffice_security import SESSION_COOKIE_NAME
from app.core.passwords import hash_password
from app.models import AuditLog, Dealer, PasswordResetToken, RoleCode, User, UserSession
from app.utils.ruc import ruc_check_digit

Sessions = async_sessionmaker[AsyncSession]

CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
BO = "/api/v1/backoffice"
# Cumple la política (sección 5.1); no contiene el usuario ni el DNI de los usuarios de prueba.
DEFAULT_PASSWORD = "Clave#2026x"

# Hash precalculado de DEFAULT_PASSWORD: Argon2 tarda, y casi todos los tests crean usuarios.
_DEFAULT_HASH = hash_password(DEFAULT_PASSWORD)


@dataclass(frozen=True)
class BoUser:
    id: uuid.UUID
    username: str
    password: str
    dni: str
    email: str
    first_names: str
    last_names: str
    role_code: str


async def create_bo_user(
    sessions: Sessions,
    *,
    username: str,
    dni: str,
    email: str,
    role_code: RoleCode = RoleCode.OPERADOR,
    password: str = DEFAULT_PASSWORD,
    password_hash: str | None = None,
    first_names: str = "María José",
    last_names: str = "Quispe Ñahui",
    phone: str | None = "987654321",
    **values: Any,
) -> BoUser:
    """Inserta un usuario vigente, activo y sin cambio de contraseña pendiente (salvo que `values` diga otra cosa)."""
    if password_hash is None:
        password_hash = _DEFAULT_HASH if password == DEFAULT_PASSWORD else hash_password(password)
    async with sessions.begin() as session:
        user = User(
            role_code=role_code.value,
            dni=dni,
            first_names=first_names,
            last_names=last_names,
            username=username,
            email=email,
            phone=phone,
            password_hash=password_hash,
            must_change_password=values.pop("must_change_password", False),
            **values,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    return BoUser(
        id=user_id,
        username=username,
        password=password,
        dni=dni,
        email=email,
        first_names=first_names,
        last_names=last_names,
        role_code=role_code.value,
    )


async def login_as(client: httpx.AsyncClient, user: BoUser, password: str | None = None) -> httpx.Response:
    return await client.post(
        f"{BO}/auth/login", json={"username": user.username, "password": password or user.password}
    )


async def logged_client(make_client: Callable[[], httpx.AsyncClient], user: BoUser) -> httpx.AsyncClient:
    """Cliente del BackOffice (fixture `make_bo_client`) con la sesión de `user` iniciada."""
    client = make_client()
    response = await login_as(client, user)
    assert response.status_code == 200, response.text
    return client


def session_cookie(client: httpx.AsyncClient) -> str | None:
    return client.cookies.get(SESSION_COOKIE_NAME)


async def get_user(sessions: Sessions, user_id: uuid.UUID) -> User:
    async with sessions() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user


async def update_user(sessions: Sessions, user_id: uuid.UUID, **values: Any) -> None:
    async with sessions.begin() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))


async def user_sessions(sessions: Sessions, user_id: uuid.UUID) -> list[UserSession]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(UserSession).where(UserSession.user_id == user_id).order_by(UserSession.created_at)
            )
        )


async def update_sessions(sessions: Sessions, user_id: uuid.UUID, **values: Any) -> None:
    async with sessions.begin() as session:
        await session.execute(update(UserSession).where(UserSession.user_id == user_id).values(**values))


async def audit_rows(sessions: Sessions, action: str | None = None) -> list[AuditLog]:
    async with sessions() as session:
        statement = select(AuditLog).order_by(AuditLog.occurred_at, AuditLog.action)
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        return list(await session.scalars(statement))


async def reset_tokens(sessions: Sessions, user_id: uuid.UUID) -> list[PasswordResetToken]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(PasswordResetToken)
                .where(PasswordResetToken.user_id == user_id)
                .order_by(PasswordResetToken.created_at)
            )
        )


# --- Dealers (fase B5) ---


def make_ruc(number: int, prefix: str = "20") -> str:
    """RUC válido (Anexo B) derivado de `number`."""
    first_ten = f"{prefix}{number:08d}"
    return f"{first_ten}{ruc_check_digit(first_ten)}"


async def create_dealer_row(sessions: Sessions, *, dealer_code: str, **values: Any) -> uuid.UUID:
    """Inserta un dealer directamente en la base (para listas y exportación)."""
    defaults: dict[str, Any] = {
        "ruc": "20100047218",
        "address": "Av. Ejemplo 123",
        "district_ubigeo": "150101",
        "contact_email": "contacto@dealer.test",
    }
    async with sessions.begin() as session:
        dealer = Dealer(dealer_code=dealer_code, **(defaults | values))
        session.add(dealer)
        await session.flush()
        return dealer.id


async def get_dealer(sessions: Sessions, dealer_id: uuid.UUID) -> Dealer:
    async with sessions() as session:
        dealer = await session.get(Dealer, dealer_id)
        assert dealer is not None
        return dealer


async def update_dealer_row(sessions: Sessions, dealer_id: uuid.UUID, **values: Any) -> None:
    async with sessions.begin() as session:
        await session.execute(update(Dealer).where(Dealer.id == dealer_id).values(**values))

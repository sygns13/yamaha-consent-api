"""Esquema del BackOffice (fase B1; secciones 4.3 a 4.6 del spec del BackOffice), contra la base de pruebas.

Criterios B1.2 a B1.6. El B1.1 (upgrade/downgrade en una base vacía) está en tests/test_bo_migrations.py.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.base import Base
from app.models import (
    SEED_TABLES,
    BackofficeModule,
    Dealer,
    Role,
    RoleModule,
    UbigeoDepartment,
    UbigeoDistrict,
    UbigeoProvince,
    User,
)
from tests.conftest import truncate_tables

Sessions = async_sessionmaker[AsyncSession]


def make_user(**overrides: Any) -> User:
    values: dict[str, Any] = {
        "role_code": "OPERADOR",
        "dni": "12345678",
        "first_names": "Juan",
        "last_names": "Pérez Gómez",
        "username": "jperez",
        "email": "jperez@dominio.com",
        "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$prueba$prueba",
    }
    values.update(overrides)
    return User(**values)


def make_dealer(**overrides: Any) -> Dealer:
    values: dict[str, Any] = {
        "dealer_code": "CL0001",
        "ruc": "20100047218",
        "address": "Av. Ejemplo 123",
        "district_ubigeo": "150101",
        "contact_email": "contacto@abcmotors.pe",
    }
    values.update(overrides)
    return Dealer(**values)


async def insert(sessions: Sessions, *rows: Any) -> None:
    async with sessions.begin() as session:
        session.add_all(rows)


async def insert_fails(sessions: Sessions, row: Any, constraint: str) -> None:
    async with sessions() as session:
        session.add(row)
        with pytest.raises(IntegrityError, match=constraint):
            await session.commit()


async def soft_delete(sessions: Sessions, model: type, row_id: uuid.UUID) -> None:
    async with sessions.begin() as session:
        await session.execute(update(model).where(model.id == row_id).values(is_deleted=1))


# --- B1.2 Semillas ---


async def test_seed_roles(session_factory: Sessions) -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(select(Role.code, Role.name, Role.description).order_by(Role.code))
        ).all()
    assert rows == [("ADMIN", "Administrador", None), ("OPERADOR", "Operador", None)]


async def test_seed_modules(session_factory: Sessions) -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    BackofficeModule.code,
                    BackofficeModule.name,
                    BackofficeModule.description,
                    BackofficeModule.sort_order,
                ).order_by(BackofficeModule.sort_order)
            )
        ).all()
    assert rows == [
        ("DEALERS", "Gestión de Dealers", "Registra y actualiza los datos de los Dealers.", 10),
        ("USUARIOS", "Administración de usuarios", "Crea y administra los usuarios del BackOffice.", 20),
        ("AUDITORIA", "Bitácora de auditoría", "Consulta las acciones registradas en el sistema.", 30),
    ]


async def test_seed_role_modules(session_factory: Sessions) -> None:
    async with session_factory() as session:
        rows = set((await session.execute(select(RoleModule.role_code, RoleModule.module_code))).all())
    assert rows == {
        ("ADMIN", "DEALERS"),
        ("ADMIN", "USUARIOS"),
        ("ADMIN", "AUDITORIA"),
        ("OPERADOR", "DEALERS"),
    }


# --- B1.3 Ubigeo ---


async def test_ubigeo_counts(session_factory: Sessions) -> None:
    async with session_factory() as session:
        departments = await session.scalar(select(func.count()).select_from(UbigeoDepartment))
        provinces = await session.scalar(select(func.count()).select_from(UbigeoProvince))
        districts = await session.scalar(select(func.count()).select_from(UbigeoDistrict))
    assert (departments, provinces, districts) == (25, 196, 1892)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("150101", ("LIMA", "LIMA", "LIMA")),
        ("070101", ("CALLAO", "CALLAO", "CALLAO")),
        ("150501", ("LIMA", "CAÑETE", "SAN VICENTE DE CAÑETE")),  # la Ñ se conserva
    ],
)
async def test_ubigeo_known_districts(
    session_factory: Sessions, code: str, expected: tuple[str, ...]
) -> None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(UbigeoDepartment.name, UbigeoProvince.name, UbigeoDistrict.name)
                .join(UbigeoProvince, UbigeoDistrict.province_code == UbigeoProvince.code)
                .join(UbigeoDepartment, UbigeoProvince.department_code == UbigeoDepartment.code)
                .where(UbigeoDistrict.code == code)
            )
        ).one()
    assert tuple(row) == expected


async def test_every_district_has_province_and_department(session_factory: Sessions) -> None:
    async with session_factory() as session:
        complete = await session.scalar(
            select(func.count())
            .select_from(UbigeoDistrict)
            .join(UbigeoProvince, UbigeoDistrict.province_code == UbigeoProvince.code)
            .join(UbigeoDepartment, UbigeoProvince.department_code == UbigeoDepartment.code)
            .where(
                func.left(UbigeoDistrict.code, 4) == UbigeoProvince.code,
                func.left(UbigeoDistrict.code, 2) == UbigeoDepartment.code,
            )
        )
        provinces_without_districts = await session.scalar(
            select(func.count())
            .select_from(UbigeoProvince)
            .where(
                ~select(UbigeoDistrict.code)
                .where(UbigeoDistrict.province_code == UbigeoProvince.code)
                .exists()
            )
        )
    assert complete == 1892
    assert provinces_without_districts == 0


async def test_ubigeo_checks_reject_inconsistent_codes(session_factory: Sessions) -> None:
    await insert_fails(
        session_factory,
        UbigeoDistrict(code="150199", province_code="1502", name="X"),
        "ck_ubigeo_districts_code_in_province",
    )
    await insert_fails(
        session_factory,
        UbigeoProvince(code="1599", department_code="14", name="X"),
        "ck_ubigeo_provinces_code_in_department",
    )
    await insert_fails(
        session_factory, UbigeoDepartment(code="AB", name="X"), "ck_ubigeo_departments_code_format"
    )


# --- B1.4 Unicidad parcial ---


@pytest.mark.parametrize(
    ("field", "constraint"),
    [
        ("username", "uq_users_active_username"),
        ("dni", "uq_users_active_dni"),
        ("email", "uq_users_active_email"),
    ],
)
async def test_two_active_users_cannot_share_a_unique_field(
    session_factory: Sessions, field: str, constraint: str
) -> None:
    first = make_user()
    await insert(session_factory, first)
    # Otro usuario que solo comparte `field` con el primero.
    others = {"username": "mlopez", "dni": "87654321", "email": "mlopez@dominio.com"}
    others[field] = getattr(first, field)
    await insert_fails(session_factory, make_user(**others), constraint)

    await soft_delete(session_factory, User, first.id)
    await insert(session_factory, make_user(**others))


async def test_dealer_code_is_unique_ignoring_case_among_active(session_factory: Sessions) -> None:
    first = make_dealer(dealer_code="CL0001")
    await insert(session_factory, first)
    await insert_fails(session_factory, make_dealer(dealer_code="cl0001"), "uq_dealers_active_code")

    await soft_delete(session_factory, Dealer, first.id)
    await insert(session_factory, make_dealer(dealer_code="cl0001"))


async def test_ruc_is_not_unique(session_factory: Sessions) -> None:
    # Varias sedes (dealerCode) pueden compartir RUC.
    await insert(session_factory, make_dealer(dealer_code="CL0001"), make_dealer(dealer_code="CL0002"))


# --- B1.5 CHECK y FK ---


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"dni": "1234567"}, "ck_users_dni_format"),
        ({"dni": "1234567A"}, "ck_users_dni_format"),
        ({"username": "Jperez"}, "ck_users_username_format"),  # mayúsculas
        ({"username": "jpe"}, "ck_users_username_format"),  # menos de 4
        ({"username": "1perez"}, "ck_users_username_format"),  # no empieza con letra
        ({"username": "j perez"}, "ck_users_username_format"),
        ({"username": "j" * 31}, "value too long"),  # más de 30: lo rechaza el largo de la columna
        ({"phone": "98765432"}, "ck_users_phone_format"),
        ({"phone": "98765432A"}, "ck_users_phone_format"),
        ({"is_deleted": 2}, "ck_users_is_deleted_bool"),
        ({"failed_login_attempts": -1}, "ck_users_failed_login_attempts_non_negative"),
        ({"role_code": "SUPERADMIN"}, "fk_users_role_code_roles"),
    ],
)
async def test_user_checks_reject_invalid_values(
    session_factory: Sessions, overrides: dict[str, Any], constraint: str
) -> None:
    async with session_factory() as session:
        session.add(make_user(**overrides))
        with pytest.raises(DBAPIError, match=constraint):
            await session.commit()


@pytest.mark.parametrize(
    "overrides",
    [
        {"dni": "00000000", "username": "admin", "phone": None},  # datos de prueba del primer Administrador
        {"username": "j.perez_1-x", "phone": "987654321"},
        {"username": "a" * 30},
    ],
)
async def test_user_checks_accept_valid_values(session_factory: Sessions, overrides: dict[str, Any]) -> None:
    await insert(session_factory, make_user(**overrides))


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"ruc": "30100047218"}, "ck_dealers_ruc_format"),  # prefijo no permitido
        ({"ruc": "2010004721"}, "ck_dealers_ruc_format"),  # 10 dígitos
        ({"ruc": "2010004721A"}, "ck_dealers_ruc_format"),
        ({"is_deleted": 2}, "ck_dealers_is_deleted_bool"),
        ({"district_ubigeo": "159999"}, "fk_dealers_district_ubigeo_ubigeo_districts"),
    ],
)
async def test_dealer_checks_reject_invalid_values(
    session_factory: Sessions, overrides: dict[str, Any], constraint: str
) -> None:
    await insert_fails(session_factory, make_dealer(**overrides), constraint)


@pytest.mark.parametrize("prefix", ["10", "15", "16", "17", "20"])
async def test_dealer_accepts_every_ruc_prefix(session_factory: Sessions, prefix: str) -> None:
    # Solo formato: el dígito verificador se valida en la aplicación (Anexo B, fase B5).
    await insert(session_factory, make_dealer(ruc=f"{prefix}123456789"))


# --- B1.6 Las semillas no se borran entre tests ---


def test_seed_tables_only_reference_seed_tables() -> None:
    # Condición para que el TRUNCATE ... CASCADE de los tests no las alcance (sección 4.6).
    assert SEED_TABLES <= set(Base.metadata.tables)
    for name in SEED_TABLES:
        referenced = {fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys}
        assert referenced <= SEED_TABLES, name


async def test_truncate_between_tests_keeps_seeds(db_engine: AsyncEngine, session_factory: Sessions) -> None:
    await insert(session_factory, make_user())
    await insert(session_factory, make_dealer())

    async with db_engine.begin() as conn:
        await truncate_tables(conn)

    async with session_factory() as session:
        counts = {
            name: await session.scalar(text(f"SELECT count(*) FROM {name}"))
            for name in (
                "users",
                "dealers",
                "roles",
                "backoffice_modules",
                "role_modules",
                "ubigeo_districts",
            )
        }
    assert counts == {
        "users": 0,
        "dealers": 0,
        "roles": 2,
        "backoffice_modules": 3,
        "role_modules": 4,
        "ubigeo_districts": 1892,
    }

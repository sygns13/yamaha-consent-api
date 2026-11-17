"""Catálogo Ubigeo del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, sección 6.7; fase B5): criterios B5.9 y B5.11.

El orden por nombre se compara solo entre los nombres ASCII: el lugar de la Ñ depende de la colación de la base (la
imagen local de PostgreSQL, sobre musl, ordena por punto de código; Cloud SQL usa las reglas del idioma).
"""

import csv
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests.bo_helpers import BO, BoUser, logged_client

UBIGEO = f"{BO}/ubigeo"
ClientFactory = Callable[[], httpx.AsyncClient]
# Los conteos esperados salen del CSV versionado que carga la migración (sección 4.8), nunca de memoria.
UBIGEO_CSV = Path(__file__).resolve().parents[1] / "alembic" / "data" / "ubigeo_inei.csv"


def csv_codes() -> list[str]:
    with UBIGEO_CSV.open(encoding="utf-8", newline="") as file:
        return [row["ubigeo"] for row in csv.DictReader(file)]


def expected_provinces(department: str) -> int:
    return len({code[:4] for code in csv_codes() if code.startswith(department)})


def expected_districts(province: str) -> int:
    return sum(1 for code in csv_codes() if code.startswith(province))


@pytest.fixture(params=["ADMIN", "OPERADOR"])
async def client(
    request: pytest.FixtureRequest, make_bo_client: ClientFactory, admin_user: BoUser, operator_user: BoUser
) -> httpx.AsyncClient:
    """B5.11: el Operador usa el catálogo igual que el Administrador (módulo DEALERS)."""
    return await logged_client(make_bo_client, admin_user if request.param == "ADMIN" else operator_user)


def assert_sorted_by_name(items: list[dict[str, str]]) -> None:
    names = [item["name"] for item in items if item["name"].isascii()]
    assert names == sorted(names)


async def test_departments(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{UBIGEO}/departments")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 25
    assert items[0] == {"code": "01", "name": "AMAZONAS"}
    assert {"code": "15", "name": "LIMA"} in items and {"code": "07", "name": "CALLAO"} in items
    assert_sorted_by_name(items)


async def test_provinces_of_a_department(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{UBIGEO}/departments/15/provinces")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == expected_provinces("15")
    assert all(item["code"].startswith("15") and len(item["code"]) == 4 for item in items)
    assert {"code": "1501", "name": "LIMA"} in items and {"code": "1505", "name": "CAÑETE"} in items
    assert items[0] == {"code": "1502", "name": "BARRANCA"}
    assert_sorted_by_name(items)

    callao = (await client.get(f"{UBIGEO}/departments/07/provinces")).json()
    assert callao == [{"code": "0701", "name": "CALLAO"}]


async def test_districts_of_a_province(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{UBIGEO}/provinces/1501/districts")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == expected_districts("1501")
    assert all(item["code"].startswith("1501") and len(item["code"]) == 6 for item in items)
    assert items[0] == {"code": "150102", "name": "ANCON"}
    assert {"code": "150101", "name": "LIMA"} in items
    assert_sorted_by_name(items)

    canete = (await client.get(f"{UBIGEO}/provinces/1505/districts")).json()
    assert {"code": "150501", "name": "SAN VICENTE DE CAÑETE"} in canete


@pytest.mark.parametrize("code", ["99", "1", "150", "AB", "00"])
async def test_unknown_department(client: httpx.AsyncClient, code: str) -> None:
    response = await client.get(f"{UBIGEO}/departments/{code}/provinces")
    assert response.status_code == 404
    assert response.json() == {"detail": "No existe el departamento.", "code": "NOT_FOUND"}


@pytest.mark.parametrize("code", ["9999", "15", "15011", "15A1"])
async def test_unknown_province(client: httpx.AsyncClient, code: str) -> None:
    response = await client.get(f"{UBIGEO}/provinces/{code}/districts")
    assert response.status_code == 404
    assert response.json() == {"detail": "No existe la provincia.", "code": "NOT_FOUND"}


async def test_ubigeo_responses_are_not_cached(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{UBIGEO}/departments")
    assert response.headers["cache-control"] == "no-store"

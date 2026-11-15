"""ubigeo inei seed

Carga el catálogo Ubigeo INEI (sección 4.8 del spec del BackOffice) desde alembic/data/ubigeo_inei.csv, generado por
scripts/build_ubigeo_csv.py desde la fuente fijada (ver alembic/data/UBIGEO_FUENTE.md): 25 departamentos, 196
provincias y 1.892 distritos. Departamentos y provincias se derivan de los distritos (primeros 2 o 4 dígitos).

Un distrito que falte (creado por ley después de la fuente) se agrega con una migración nueva, no editando el CSV.

Downgrade (decisión del responsable, 15/11/2026): TRUNCATE ... CASCADE de las tres tablas de ubigeo, que vacía también
`dealers` (cada dealer referencia un distrito). Así el rollback documentado (`alembic downgrade b7c2d9e41f60`, que
borra los datos del BackOffice) no falla cuando ya hay dealers registrados.

Revision ID: b6b72cd353a6
Revises: 56b3050c235a
Create Date: 2026-11-15 15:40:46.280855

"""
import csv
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6b72cd353a6'
down_revision: Union[str, Sequence[str], None] = '56b3050c235a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Ruta relativa a este archivo: el Dockerfile copia alembic/ completo, así que el CSV viaja en la imagen.
DATA_FILE = Path(__file__).resolve().parents[1] / 'data' / 'ubigeo_inei.csv'
HEADER = ['ubigeo', 'departamento', 'provincia', 'distrito']


def read_ubigeo() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Departamentos, provincias y distritos del CSV, listos para op.bulk_insert. Los códigos se leen como texto."""
    with DATA_FILE.open(newline='', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != HEADER:
            raise RuntimeError(f'Encabezado inesperado en {DATA_FILE.name}: {reader.fieldnames}')
        rows = list(reader)

    departments: dict[str, str] = {}
    provinces: dict[str, str] = {}
    districts = []
    for row in rows:
        code = row['ubigeo']
        departments.setdefault(code[:2], row['departamento'])
        provinces.setdefault(code[:4], row['provincia'])
        districts.append({'code': code, 'province_code': code[:4], 'name': row['distrito']})
    return (
        [{'code': code, 'name': name} for code, name in sorted(departments.items())],
        [
            {'code': code, 'department_code': code[:2], 'name': name}
            for code, name in sorted(provinces.items())
        ],
        districts,
    )


def upgrade() -> None:
    """Upgrade schema."""
    # Tipos de enlace: String, no CHAR (sa.CHAR sin largo se castea a char(1) y truncaría los códigos).
    departments, provinces, districts = read_ubigeo()
    op.bulk_insert(
        sa.table('ubigeo_departments', sa.column('code', sa.String), sa.column('name', sa.String)),
        departments,
    )
    op.bulk_insert(
        sa.table(
            'ubigeo_provinces',
            sa.column('code', sa.String),
            sa.column('department_code', sa.String),
            sa.column('name', sa.String),
        ),
        provinces,
    )
    op.bulk_insert(
        sa.table(
            'ubigeo_districts',
            sa.column('code', sa.String),
            sa.column('province_code', sa.String),
            sa.column('name', sa.String),
        ),
        districts,
    )


def downgrade() -> None:
    """Downgrade schema: vacía las tres tablas de ubigeo (y, por CASCADE, dealers)."""
    op.execute('TRUNCATE ubigeo_districts, ubigeo_provinces, ubigeo_departments CASCADE')

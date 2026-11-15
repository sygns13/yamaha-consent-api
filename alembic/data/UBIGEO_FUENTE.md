# Fuente del catálogo Ubigeo INEI

Generado por `scripts/build_ubigeo_csv.py` (fase B1 del BackOffice; sección 4.8 de
`requerimientos/BACKOFFICE_DEALERS.spec.md`). No editar a mano: si hace falta un distrito nuevo, se agrega con una
migración nueva de Alembic (ver "Limitación y mantenimiento").

## Fuente

| Dato | Valor |
|---|---|
| Repositorio | `jmcastagnetto/ubigeo-peru-aumentado` (GitHub). Dataset **comunitario**, no publicado por el INEI |
| Archivo | `ubigeo_distrito.csv` |
| URL fijada al commit | https://raw.githubusercontent.com/jmcastagnetto/ubigeo-peru-aumentado/5e98222bb010243592ae02bfafb0a3658d6c4bd8/ubigeo_distrito.csv |
| Commit | `5e98222bb010243592ae02bfafb0a3658d6c4bd8` (24/08/2021) |
| Fecha de descarga | 15/11/2026 |
| SHA-256 del archivo original | `6a2cde8fb82e336285da4d684241450e8a36630e42799652c8d72b596ec252e4` |
| SHA-256 de `ubigeo_inei.csv` | `32da902c2b3ca6b44a84cfb1f8650a09f6117b6c2cf88c5d0e2a118650129926` |
| SHA-256 de `LICENSE` (mismo commit) | `0e720d3ee99b337850edf241b74abb8c71b3a45677afdd7af4263d0c700e8ca4` |
| Licencia | MIT (texto completo al final) |

Formato observado del original: CSV UTF-8 sin BOM, con encabezado, fin de línea LF. Registros con un
salto de línea dentro de un campo entre comillas: `010519`, `010610`.

## Transformación

1. Descarga de la URL fijada y verificación del SHA-256 (si no coincide, el script se detiene).
2. Lectura con `csv.DictReader` (UTF-8). El código `inei` se trata siempre como texto (conserva los ceros a la
   izquierda). Se usan solo las columnas `inei` (→ `ubigeo`), `departamento`, `provincia` y `distrito`; las demás se
   descartan.
3. Se excluyen los registros cuyo `inei` no tiene 6 dígitos (ver "Registro excluido").
4. Trim y normalización Unicode NFC de los nombres. Los nombres se conservan tal como vienen en la fuente (mayúsculas;
   con tilde o Ñ solo donde la fuente la trae). Nombres con letras no ASCII distintas de la Ñ: `080918` (distrito): UNION ASHÁNINKA.
5. Validaciones: códigos únicos; un solo nombre por departamento (2 dígitos) y por provincia (4 dígitos); nombres no
   vacíos y dentro del largo de las columnas (máximo observado: departamento 13, provincia 25, distrito 36).
6. Salida `ubigeo_inei.csv`: UTF-8 sin BOM, fin de línea LF, encabezado `ubigeo,departamento,provincia,distrito`, ordenado por
   `ubigeo`. Departamentos y provincias se derivan de los distritos: código = primeros 2 o 4 dígitos; nombre = columna
   `departamento` o `provincia`.

## Conteos

| Concepto | Cantidad |
|---|---|
| Registros leídos | 1893 |
| Registros excluidos | 1 |
| Distritos | 1892 |
| Provincias | 196 |
| Departamentos | 25 |

## Registro excluido (faltante conocido)

| `inei` | `reniec` | Departamento | Provincia | Distrito |
|---|---|---|---|---|
| `NA` | `170107` | MOQUEGUA | MARISCAL NIETO | SAN ANTONIO |

Su código INEI **no se inventa**. Si un Dealer está en ese distrito, el responsable confirma el código INEI con una
fuente oficial (INEI o la ley de creación del distrito) y se agrega con una migración nueva.

## Limitación y mantenimiento

El archivo no cambia desde agosto de 2021 (al 15/11/2026, el commit fijado es el único que lo modifica), así que puede
faltar un distrito creado después. Si un Dealer está en un distrito que no aparece: el responsable confirma el código
INEI con una fuente oficial y se agrega con una migración nueva de Alembic. No hay actualización automática. Revisión
sugerida: una vez al año.

## Licencia

Texto completo del archivo `LICENSE` del repositorio en el commit fijado (la licencia exige conservar el aviso de
copyright):

```text
MIT License

Copyright (c) 2021 Jesus M. Castagnetto

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

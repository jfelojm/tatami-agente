"""
Promueve filas listas en BD_ITEMS_PENDIENTES hacia BD_ITEMS_PROV.

Automatiza lo repetible:
  - cod_item_prov, cod_proveedor, cod_mp_sistema, descripcion_proveedor (desde pendientes)
  - nombre_mp, unidad_base_sistema desde BD_MP_SISTEMA según cod_mp_asignado
  - factor_conversion = 1 (una unidad de compra = una unidad base; revisar si venden por bulto/caja)
  - unidad_compra = unidad_base por defecto (misma lógica; el usuario ajusta si compran en otra medida)
  - activo = SI

Requiere intervención humana cuando la compra no es 1:1 con la unidad base (factor distinto,
unidad de compra distinta, bodega destino, etc.): conviene corregir la fila nueva en BD_ITEMS_PROV.

Solo procesa filas con estado PENDIENTE y cod_mp_asignado no vacío.
Tras insertar, marca la fila en pendientes como REGISTRADO (si no es --dry-run).
"""

from __future__ import annotations

import argparse
import os

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

from codigo_factura_match import (
    cod_item_prov_para_catalogo,
    cod_proveedores_strip_sufijo_desde_bd_prov,
    normalizar_cod_item_para_match,
    normalizar_cod_proveedor_para_match,
)

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_PEND = "BD_ITEMS_PENDIENTES"
SHEET_PROV = "BD_ITEMS_PROV"
SHEET_MP = "BD_MP_SISTEMA"


def _auth():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _prov_headers_and_rows(values: list[list[str]]) -> tuple[list[str], list[list[str]]] | None:
    hi = _find_header_row(values, "cod_item_prov")
    if hi is None:
        return None
    headers = [(c or "").strip() for c in values[hi]]
    # Salta fila leyenda típica [FK]/[LINK]/[PK]
    data_start = hi + 2
    rows = values[data_start:]
    return headers, rows


def _find_header_row(values: list[list[str]], marker: str) -> int | None:
    for i, row in enumerate(values):
        if any((c or "").strip() == marker for c in row):
            return i
    return None


def _fila_tiene_datos_catalogo(
    row: list[str],
    ic_item: int,
    ic_prov: int,
    ic_mp: int,
) -> bool:
    if ic_item >= 0 and ic_item < len(row) and (row[ic_item] or "").strip():
        return True
    if ic_prov >= 0 and ic_prov < len(row) and (row[ic_prov] or "").strip():
        return True
    if ic_mp >= 0 and ic_mp < len(row) and (row[ic_mp] or "").strip():
        return True
    return False


def _find_next_row_items_prov(
    values: list[list[str]], hi: int, headers: list[str]
) -> int:
    """Fila 1-based donde insertar (después del último registro con datos)."""
    ic_item = headers.index("cod_item_prov") if "cod_item_prov" in headers else -1
    ic_prov = headers.index("cod_proveedor") if "cod_proveedor" in headers else -1
    ic_mp = headers.index("cod_mp_sistema") if "cod_mp_sistema" in headers else -1
    last_data_idx = hi
    for i in range(hi + 1, len(values)):
        row = values[i]
        if not row or (row and str(row[0]).strip().startswith("[")):
            continue
        if _fila_tiene_datos_catalogo(row, ic_item, ic_prov, ic_mp):
            last_data_idx = i
    return last_data_idx + 2


def _cargar_items_prov_dicts(values: list[list[str]]) -> tuple[list[str], list[dict]]:
    parsed = _prov_headers_and_rows(values)
    if not parsed:
        return [], []
    headers, rows = parsed
    out: list[dict] = []
    for row in rows:
        if not any((c or "").strip() for c in row):
            continue
        if row and str(row[0]).strip().startswith("["):
            continue
        d = {
            headers[j]: (row[j] if j < len(row) else "").strip()
            for j in range(len(headers))
        }
        out.append(d)
    return headers, out


def _ya_existe(
    items: list[dict],
    cod_prov: str,
    cod_item_xml: str,
    razon_social: str,
    ruc: str,
) -> bool:
    want = normalizar_cod_item_para_match(cod_item_xml, razon_social, ruc)
    for it in items:
        if normalizar_cod_proveedor_para_match(it.get("cod_proveedor") or "") != normalizar_cod_proveedor_para_match(cod_prov):
            continue
        got = normalizar_cod_item_para_match(
            it.get("cod_item_prov") or "", razon_social, ruc
        )
        if got == want:
            return True
    return False


def _cargar_mp_lookup(values: list[list[str]]) -> dict[str, dict[str, str]]:
    hi = _find_header_row(values, "cod_mp_sistema")
    if hi is None:
        return {}
    headers = [(c or "").strip() for c in values[hi]]

    def ix(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    ic = ix("cod_mp_sistema")
    if ic is None:
        return {}
    inom = ix("nombre_mp")
    iu = ix("unidad_base")

    out: dict[str, dict[str, str]] = {}
    for row in values[hi + 1 :]:
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        cod = row[ic].strip() if ic < len(row) else ""
        if not cod:
            continue
        out[cod] = {
            "nombre_mp": row[inom].strip() if inom is not None and inom < len(row) else "",
            "unidad_base": row[iu].strip() if iu is not None and iu < len(row) else "",
        }
    return out


def _leer_pendientes(ws: gspread.Worksheet) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    values = ws.get_all_values()
    hi = _find_header_row(values, "clave_unica")
    if hi is None:
        hi = _find_header_row(values, "cod_item_xml")
    if hi is None:
        return [], []

    headers = [(c or "").strip() for c in values[hi]]
    filas: list[tuple[int, dict[str, str]]] = []
    # fila 1-based en Sheets
    for i in range(hi + 1, len(values)):
        row = values[i]
        sheet_row = i + 1
        d: dict[str, str] = {}
        for j, h in enumerate(headers):
            if not h:
                continue
            d[h] = (row[j] if j < len(row) else "").strip()
        filas.append((sheet_row, d))
    return headers, filas


def _armar_fila_prov(
    headers_prov: list[str],
    pend: dict[str, str],
    mp: dict[str, str],
    *,
    cod_proveedores_strip: frozenset[str] | None = None,
) -> list[str]:
    """Construye lista en el orden de columnas de BD_ITEMS_PROV."""
    cod_mp = (pend.get("cod_mp_asignado") or "").strip()
    ub = (mp.get("unidad_base") or "").strip()
    razon = (pend.get("razon_social") or "").strip()
    ruc = (pend.get("ruc_proveedor") or "").strip()
    cod_prov = (pend.get("cod_proveedor") or "").strip()
    cod_xml = (pend.get("cod_item_xml") or "").strip()
    cod_catalogo = cod_item_prov_para_catalogo(
        cod_xml,
        razon_social=razon,
        ruc=ruc,
        cod_proveedor=cod_prov,
        cod_proveedores_strip=cod_proveedores_strip,
    )

    valores: dict[str, str] = {
        "cod_item_prov": cod_catalogo,
        "cod_proveedor": cod_prov,
        "cod_mp_sistema": cod_mp,
        "descripcion_proveedor": (pend.get("descripcion_xml") or "").strip(),
        "activo": "SI",
        "factor_conversion": "1",
    }
    if "nombre_mp" in headers_prov:
        valores["nombre_mp"] = (mp.get("nombre_mp") or "").strip()
    if "unidad_base_sistema" in headers_prov:
        valores["unidad_base_sistema"] = ub
    if "unidad_compra" in headers_prov:
        # Por defecto igual que base; el usuario cambia a CAJA/BOLSA/etc.
        valores["unidad_compra"] = ub
    # No tocamos precio_ref / fecha_precio_ref / cod_bodega_destino: los completa operación

    return [valores.get(h, "") for h in headers_prov]


def run(*, dry_run: bool) -> int:
    sh = _auth()
    try:
        ws_p = sh.worksheet(SHEET_PEND)
    except gspread.exceptions.WorksheetNotFound:
        print(f"ERROR: no existe la hoja {SHEET_PEND}")
        return 2

    ws_prov = sh.worksheet(SHEET_PROV)
    ws_mp = sh.worksheet(SHEET_MP)

    headers_pend, filas_pend = _leer_pendientes(ws_p)
    if not headers_pend:
        print("ERROR: no se encontró cabecera (clave_unica / cod_item_xml) en BD_ITEMS_PENDIENTES")
        return 2

    try:
        idx_estado = headers_pend.index("estado")
    except ValueError:
        print("ERROR: columna 'estado' no encontrada en BD_ITEMS_PENDIENTES")
        return 2

    try:
        idx_cod_mp = headers_pend.index("cod_mp_asignado")
    except ValueError:
        print("ERROR: columna 'cod_mp_asignado' no encontrada en BD_ITEMS_PENDIENTES")
        return 2

    vals_prov = ws_prov.get_all_values()
    hi_prov = _find_header_row(vals_prov, "cod_item_prov")
    headers_prov, items_prov_list = _cargar_items_prov_dicts(vals_prov)
    if not headers_prov or hi_prov is None:
        print("ERROR: no se encontró cabecera cod_item_prov en BD_ITEMS_PROV")
        return 2

    mp_lookup = _cargar_mp_lookup(ws_mp.get_all_values())
    if not mp_lookup:
        print("WARN: BD_MP_SISTEMA vacío o sin cod_mp_sistema; no se podrá rellenar nombre/unidad.")

    try:
        vals_bd_prov = sh.worksheet("BD_PROV").get_all_values()
        cod_proveedores_strip = cod_proveedores_strip_sufijo_desde_bd_prov(vals_bd_prov)
    except Exception:
        cod_proveedores_strip = frozenset()
    if cod_proveedores_strip:
        print(f"  Proveedores con código sin sufijo -N (COLEMUN, etc.): {len(cod_proveedores_strip)}")

    insertadas = 0
    omitidas = 0
    errores = 0

    for sheet_row, pend in filas_pend:
        estado = (pend.get("estado") or "").strip().upper()
        if estado != "PENDIENTE":
            continue

        cod_mp = (pend.get("cod_mp_asignado") or "").strip()
        cod_prov = (pend.get("cod_proveedor") or "").strip()
        cod_xml = (pend.get("cod_item_xml") or "").strip()
        razon = (pend.get("razon_social") or "").strip()
        ruc = (pend.get("ruc_proveedor") or "").strip()

        if not cod_mp or not cod_prov or not cod_xml:
            continue

        if _ya_existe(items_prov_list, cod_prov, cod_xml, razon, ruc):
            print(f"  SKIP fila {sheet_row}: ya existe en BD_ITEMS_PROV ({cod_prov} / {cod_xml})")
            omitidas += 1
            if not dry_run:
                col_estado = idx_estado + 1
                rng = rowcol_to_a1(sheet_row, col_estado)
                ws_p.update(
                    range_name=rng,
                    values=[["REGISTRADO"]],
                    value_input_option=ValueInputOption.user_entered,
                )
            continue

        mp = mp_lookup.get(cod_mp)
        if not mp:
            print(f"  WARN fila {sheet_row}: cod_mp_asignado={cod_mp} no está en BD_MP_SISTEMA — omitido")
            errores += 1
            continue

        nueva = _armar_fila_prov(
            headers_prov, pend, mp, cod_proveedores_strip=cod_proveedores_strip
        )
        cod_cat = nueva[headers_prov.index("cod_item_prov")] if "cod_item_prov" in headers_prov else ""

        if dry_run:
            extra = f" (catálogo {cod_cat})" if cod_cat and cod_cat != cod_xml else ""
            print(f"  [DRY RUN] fila {sheet_row}: insertaría {cod_prov} | {cod_xml}{extra} -> {cod_mp}")
            insertadas += 1
            continue

        filas_nuevas.append(nueva)
        items_prov_list.append(
            {headers_prov[j]: nueva[j] for j in range(len(headers_prov))}
        )

        col_estado = idx_estado + 1
        rng = rowcol_to_a1(sheet_row, col_estado)
        ws_p.update(
            range_name=rng,
            values=[["REGISTRADO"]],
            value_input_option=ValueInputOption.user_entered,
        )

        extra = f" cod_item_prov={cod_cat}" if cod_cat and cod_cat != cod_xml else ""
        print(f"  OK fila {sheet_row}: alta BD_ITEMS_PROV | {cod_prov} | {cod_xml}{extra} -> {cod_mp}")
        insertadas += 1

    if not dry_run and filas_nuevas:
        start_row = _find_next_row_items_prov(vals_prov, hi_prov, headers_prov)
        end_row = start_row + len(filas_nuevas) - 1
        ws_prov.update(
            range_name=f"A{start_row}",
            values=filas_nuevas,
            value_input_option=ValueInputOption.user_entered,
        )
        print(f"  → BD_ITEMS_PROV: escritas filas {start_row}-{end_row}")

    print(
        f"\nListo: insertadas={insertadas} omitidas_duplicado={omitidas} "
        f"errores_sin_mp={errores} dry_run={dry_run}"
    )
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="No escribe Sheets; solo muestra qué insertaría",
    )
    args = p.parse_args()
    raise SystemExit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()

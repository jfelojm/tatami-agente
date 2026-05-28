"""
Crea/actualiza formularios de subrecetas en staging:
  STAGING_SUB_CAB      — cabecera (rendimiento, unidad, activa)
  STAGING_SUB_DETALLE  — detalle por lote (MP o subreceta hijo)

Mejoras:
  - STAGING_SUB_CAB: celda con próximo cod_subreceta (secuencia maestro)
  - STAGING_SUB_DETALLE: nombre_subreceta_hijo = listado BD_SUBRECETAS (activas)

Uso:
  python setup_staging_subrecetas.py
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from staging_common import (
    batch_format,
    cargar_filas_mp_maestro,
    cargar_filas_subrecetas_maestro,
    crear_hoja_si_no_existe,
    dropdown_list,
    dropdown_range,
    escribir_aux_hoja,
    estado_conditional,
    header_style,
    hide_sheet,
    proximo_cod_subreceta_desde_maestro,
    sheets_api,
    staging_spreadsheet_id,
)

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHEET_CAB = "STAGING_SUB_CAB"
SHEET_DET = "STAGING_SUB_DETALLE"
AUX_MP = "_AUX_MP_SUB"
AUX_SUB_BD = "_AUX_BD_SUBRECETAS"
AUX_SUB_HIJOS_LEGACY = "_AUX_SUB_HIJOS"  # pestaña vieja; se rellena igual que AUX_SUB_BD
AUX_PADRE_STAGING = "_AUX_SUB_PADRE_STAGING"
AUX_PADRES_UNION = "_AUX_SUB_PADRES_UNION"

HEADERS_CAB = [
    "nombre_subreceta",
    "cod_subreceta",
    "rendimiento_estandar",
    "unidad",
    "activa",
    "notas",
    "estado",
]

HEADERS_DET = [
    "nombre_subreceta_padre",
    "cod_subreceta_padre",
    "tipo_linea",
    "nombre_mp",
    "cod_mp_sistema",
    "nombre_subreceta_hijo",
    "cod_subreceta_hijo",
    "cantidad",
    "unidad_base",
    "cod_bodega",
    "merma_pct",
    "estado",
]


def _configurar_aux_bd_subrecetas(sheets) -> tuple[str, int]:
    """Copia BD_SUBRECETAS al staging (sin IMPORTRANGE)."""
    filas = cargar_filas_subrecetas_maestro(solo_activas=True)
    header = ["nombre_subreceta", "cod_subreceta"]
    for nombre in (AUX_SUB_BD, AUX_SUB_HIJOS_LEGACY):
        escribir_aux_hoja(sheets, nombre, header, filas)
    log.info(
        "Aux %s (+ %s legacy): %s subrecetas",
        AUX_SUB_BD,
        AUX_SUB_HIJOS_LEGACY,
        len(filas),
    )
    return AUX_SUB_BD, len(filas)


def _configurar_aux_mp(sheets) -> tuple[str, int]:
    """Copia nombres MP únicos al staging (sin IMPORTRANGE)."""
    filas = cargar_filas_mp_maestro()
    escribir_aux_hoja(
        sheets,
        AUX_MP,
        ["nombre_mp", "cod_mp_sistema"],
        filas,
    )
    log.info("Aux %s: %s MPs", AUX_MP, len(filas))
    return AUX_MP, len(filas)


def _configurar_aux_padres_union(
    sheets, aux_sub_bd: str, aux_padre_staging: str
) -> str:
    """Un solo listado: BD_SUBRECETAS + filas STAGING_SUB_CAB."""
    staging_id = staging_spreadsheet_id()
    sid = crear_hoja_si_no_existe(sheets, staging_id, AUX_PADRES_UNION)
    formula = (
        f"=UNIQUE(FILTER({{{aux_sub_bd}!A2:A300;{aux_padre_staging}!A2:A100}};"
        f"{{{aux_sub_bd}!A2:A300;{aux_padre_staging}!A2:A100}}<>\"\"))"
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{AUX_PADRES_UNION}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]},
    ).execute()
    hide_sheet(sheets, staging_id, sid)
    return AUX_PADRES_UNION


def _configurar_aux_padre_staging(sheets) -> str:
    """Nombres de subrecetas en STAGING_SUB_CAB (filas en edición)."""
    staging_id = staging_spreadsheet_id()
    sid = crear_hoja_si_no_existe(sheets, staging_id, AUX_PADRE_STAGING)
    formula = (
        f'=IFERROR(QUERY({SHEET_CAB}!A:B;"SELECT Col1 WHERE Col1 <> \'nombre_subreceta\' '
        f'AND Col1 IS NOT NULL ORDER BY Col1 ASC";0);"")'
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{AUX_PADRE_STAGING}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]},
    ).execute()
    hide_sheet(sheets, staging_id, sid)
    return AUX_PADRE_STAGING


def configurar_cab(sheets, proximo_cod: str) -> int:
    staging_id = staging_spreadsheet_id()
    sheet_id = crear_hoja_si_no_existe(sheets, staging_id, SHEET_CAB)
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{SHEET_CAB}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [HEADERS_CAB]},
    ).execute()

    # Referencia secuencia (columna I) — último en maestro fue 050 → siguiente 051
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{SHEET_CAB}!I1",
        valueInputOption="USER_ENTERED",
        body={
            "values": [
                [f"Próximo cod_subreceta sugerido: {proximo_cod}"],
                ["Escribe en col B el código nuevo (3 dígitos, ej. 051)"],
            ]
        },
    ).execute()

    reqs = list(header_style(sheet_id, len(HEADERS_CAB)))
    reqs.append(dropdown_list(sheet_id, 3, ["gr", "ml", "uni"]))
    reqs.append(dropdown_list(sheet_id, 4, ["SI", "NO"]))
    reqs.append(
        dropdown_list(sheet_id, 6, ["PENDIENTE", "APROBADO", "RECHAZADO", "PROMOVIDO"])
    )
    reqs.extend(estado_conditional(sheet_id, 6))

    # Resaltar col B (cod) e I (referencia)
    reqs.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 2,
                    "startColumnIndex": 8,
                    "endColumnIndex": 10,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.7},
                        "textFormat": {"bold": True, "fontSize": 10},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        }
    )

    batch_format(sheets, staging_id, reqs)
    return sheet_id


def configurar_det(
    sheets,
    aux_mp: str,
    aux_sub_bd: str,
    aux_padre_staging: str,
    aux_padres_union: str,
    *,
    n_mp: int,
    n_sub: int,
) -> int:
    staging_id = staging_spreadsheet_id()
    sheet_id = crear_hoja_si_no_existe(sheets, staging_id, SHEET_DET)
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{SHEET_DET}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [HEADERS_DET]},
    ).execute()

    mp_end = max(2, min(n_mp + 1, 999))
    sub_end = max(2, min(n_sub + 1, 999))
    cab_end = 100

    # B: cod_subreceta_padre (cabecera staging o maestro)
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{SHEET_DET}!B2",
        valueInputOption="USER_ENTERED",
        body={
            "values": [
                [
                    f'=ARRAYFORMULA(SI($A2:$A="";"";SI.ERROR(BUSCARV($A2:$A;'
                    f'{SHEET_CAB}!$A$2:$B${cab_end};2;FALSO);'
                    f'SI.ERROR(BUSCARV($A2:$A;{aux_sub_bd}!$A$2:$B${sub_end};2;FALSO);""))))'
                ]
            ]
        },
    ).execute()

    # E: cod_mp (solo si tipo_linea = MP)
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{SHEET_DET}!E2",
        valueInputOption="USER_ENTERED",
        body={
            "values": [
                [
                    f'=ARRAYFORMULA(SI($C2:$C="MP";SI($D2:$D="";"";'
                    f'SI.ERROR(BUSCARV($D2:$D;{aux_mp}!$A$2:$B${mp_end};2;FALSO);'
                    f'"MP no encontrada"));""))'
                ]
            ]
        },
    ).execute()

    # G: cod_subreceta_hijo (solo si tipo_linea = SUB)
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{SHEET_DET}!G2",
        valueInputOption="USER_ENTERED",
        body={
            "values": [
                [
                    f'=ARRAYFORMULA(SI($C2:$C="SUB";SI($F2:$F="";"";'
                    f'SI.ERROR(BUSCARV($F2:$F;{aux_sub_bd}!$A$2:$B${sub_end};2;FALSO);'
                    f'"SUB no encontrada"));""))'
                ]
            ]
        },
    ).execute()

    reqs = list(header_style(sheet_id, len(HEADERS_DET)))

    # A: nombre_subreceta_padre (union cab + maestro)
    reqs.append(dropdown_range(sheet_id, 0, f"{aux_padres_union}!$A$2:$A$400"))

    reqs.append(dropdown_list(sheet_id, 2, ["MP", "SUB"]))
    reqs.append(dropdown_range(sheet_id, 3, f"{aux_mp}!$A$2:$A${mp_end}"))

    # F: nombre_subreceta_hijo — listado BD_SUBRECETAS (col A)
    reqs.append(dropdown_range(sheet_id, 5, f"{aux_sub_bd}!$A$2:$A${sub_end}"))

    reqs.append(dropdown_list(sheet_id, 9, ["BOD-001", "BOD-002", "BOD-005"]))
    reqs.append(
        dropdown_list(sheet_id, 11, ["PENDIENTE", "APROBADO", "RECHAZADO", "PROMOVIDO"])
    )
    reqs.extend(estado_conditional(sheet_id, 11))
    batch_format(sheets, staging_id, reqs)
    return sheet_id


def main():
    proximo = proximo_cod_subreceta_desde_maestro()
    sheets = sheets_api()

    aux_sub_bd, n_sub = _configurar_aux_bd_subrecetas(sheets)
    aux_mp, n_mp = _configurar_aux_mp(sheets)
    aux_padre = _configurar_aux_padre_staging(sheets)
    aux_union = _configurar_aux_padres_union(sheets, aux_sub_bd, aux_padre)

    configurar_cab(sheets, proximo)
    configurar_det(
        sheets,
        aux_mp,
        aux_sub_bd,
        aux_padre,
        aux_union,
        n_mp=n_mp,
        n_sub=n_sub,
    )

    url = f"https://docs.google.com/spreadsheets/d/{staging_spreadsheet_id()}"
    print("\n" + "=" * 65)
    print("  OK  Staging subrecetas (actualizado)")
    print(f"  URL: {url}")
    print(f"  Proximo cod_subreceta sugerido: {proximo}")
    print(f"  _AUX_MP_SUB: {n_mp} MPs | _AUX_BD_SUBRECETAS: {n_sub} subrecetas")
    print("  Listas copiadas del maestro (no requiere autorizar IMPORTRANGE).")
    print("  Si cambia el maestro, vuelve a ejecutar: python setup_staging_subrecetas.py")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()

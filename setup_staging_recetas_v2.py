"""
Crea/actualiza STAGING_RECETAS v2 (MP + SUB) en el spreadsheet de staging.

Columnas A–N: ver ESQUEMA_RECETAS_SUBRECETAS.md §6.

Uso:
  python setup_staging_recetas_v2.py
  python setup_staging_recetas_v2.py --hoja STAGING_RECETAS_V2
"""

from __future__ import annotations

import argparse
import logging
import os

from dotenv import load_dotenv

from staging_common import (
    batch_format,
    creds,
    crear_hoja_si_no_existe,
    dropdown_list,
    dropdown_range,
    estado_conditional,
    header_style,
    hide_sheet,
    master_spreadsheet_id,
    sheets_api,
    staging_spreadsheet_id,
)

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_SHEET = "STAGING_RECETAS"
HEADERS = [
    "nombre_receta",
    "cod_receta",
    "variedad_smart_menu",
    "tipo_linea",
    "nombre_mp",
    "cod_mp_sistema",
    "nombre_subreceta",
    "cod_subreceta",
    "cantidad",
    "unidad_base",
    "cod_bodega",
    "merma_pct",
    "es_opcional",
    "pct_aplicacion",
    "estado",
]


def _aux_mp(sheets, master_id: str) -> tuple[str, int]:
    name = "_AUX_MP"
    sid = crear_hoja_si_no_existe(sheets, staging_spreadsheet_id(), name)
    formula = (
        f'=IFERROR(QUERY(IMPORTRANGE("{master_id}";"BD_MP_SISTEMA!A:L");'
        f'"SELECT Col1 WHERE Col1 <> \'nombre_mp\' ORDER BY Col1 ASC";0);'
        f'"⚠ Autoriza IMPORTRANGE")'
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_spreadsheet_id(),
        range=f"{name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]},
    ).execute()
    hide_sheet(sheets, staging_spreadsheet_id(), sid)
    return name, sid


def _aux_sub(sheets, master_id: str) -> tuple[str, int]:
    name = "_AUX_SUB"
    sid = crear_hoja_si_no_existe(sheets, staging_spreadsheet_id(), name)
    formula = (
        f'=IFERROR(QUERY(IMPORTRANGE("{master_id}";"BD_SUBRECETAS!A:E");'
        f'"SELECT Col1 WHERE Col1 <> \'nombre_subreceta\' AND Col5 = \'SI\' '
        f'ORDER BY Col1 ASC";0);'
        f'"⚠ Autoriza IMPORTRANGE")'
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_spreadsheet_id(),
        range=f"{name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]},
    ).execute()
    hide_sheet(sheets, staging_spreadsheet_id(), sid)
    return name, sid


def configurar(*, sheet_name: str) -> None:
    master_id = master_spreadsheet_id()
    staging_id = staging_spreadsheet_id()
    sheets = sheets_api()
    sheet_id = crear_hoja_si_no_existe(sheets, staging_id, sheet_name)

    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [HEADERS]},
    ).execute()

    # Referencia próximo cod_receta (columna Q)
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{sheet_name}!Q1",
        valueInputOption="USER_ENTERED",
        body={"values": [["Próximo cod_receta: (consultar BD_RECETAS_DETALLE en maestro)"]]},
    ).execute()

    # Fórmulas cod / unidad (E=MP nombre, F=cod mp, G=nombre sub, H=cod sub, I=cant, J=unidad)
    formulas = [
        [
            f'=ARRAYFORMULA(SI($A2:$A="";"";SI($D2:$D="MP";'
            f'IFERROR(VLOOKUP($E2:$E;IMPORTRANGE("{master_id}";"BD_MP_SISTEMA!A:B");2;0);"");"")))'
        ],
        [
            f'=ARRAYFORMULA(SI($A2:$A="";"";SI($D2:$D="SUB";'
            f'IFERROR(VLOOKUP($G2:$G;IMPORTRANGE("{master_id}";"BD_SUBRECETAS!A:B");2;0);"");"")))'
        ],
        [
            f'=ARRAYFORMULA(SI($A2:$A="";"";SI($D2:$D="MP";'
            f'IFERROR(VLOOKUP($F2:$F;IMPORTRANGE("{master_id}";"BD_MP_SISTEMA!A:E");5;0);'
            f'IF($D2:$D="SUB";IFERROR(VLOOKUP($H2:$H;IMPORTRANGE("{master_id}";"BD_SUBRECETAS!A:E");4;0);"");""))))'
        ],
    ]
    for col_letter, vals in zip("FHJ", formulas):
        sheets.spreadsheets().values().update(
            spreadsheetId=staging_id,
            range=f"{sheet_name}!{col_letter}2",
            valueInputOption="USER_ENTERED",
            body={"values": vals},
        ).execute()

    aux_mp, _ = _aux_mp(sheets, master_id)
    aux_sub, _ = _aux_sub(sheets, master_id)

    n = len(HEADERS)
    reqs: list[dict] = []
    reqs.extend(header_style(sheet_id, n))
    reqs.append(dropdown_list(sheet_id, 3, ["MP", "SUB"]))  # D tipo_linea
    reqs.append(dropdown_range(sheet_id, 4, f"{aux_mp}!$A$1:$A$800"))  # E nombre_mp
    reqs.append(dropdown_range(sheet_id, 6, f"{aux_sub}!$A$1:$A$200"))  # G nombre_sub
    reqs.append(dropdown_list(sheet_id, 10, ["BOD-001", "BOD-002"]))  # K cod_bodega
    reqs.append(dropdown_list(sheet_id, 12, ["NO", "SI"], strict=True))  # M es_opcional
    reqs.append(
        dropdown_list(
            sheet_id,
            14,
            ["PENDIENTE", "APROBADO", "RECHAZADO", "PROMOVIDO"],
            strict=True,
        )
    )
    reqs.extend(estado_conditional(sheet_id, 14))

    anchos = [200, 90, 140, 70, 200, 90, 200, 70, 80, 60, 90, 70, 80, 90, 110]
    for i, w in enumerate(anchos):
        reqs.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": i,
                        "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": w},
                    "fields": "pixelSize",
                }
            }
        )

    batch_format(sheets, staging_id, reqs)
    log.info("Hoja %s configurada en staging %s", sheet_name, staging_id)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--hoja",
        default=DEFAULT_SHEET,
        help=f"Nombre pestaña (default {DEFAULT_SHEET}; usar STAGING_RECETAS_V2 si conviven con v1)",
    )
    args = p.parse_args()
    configurar(sheet_name=args.hoja.strip())
    url = f"https://docs.google.com/spreadsheets/d/{staging_spreadsheet_id()}"
    print("\n" + "=" * 65)
    print("  OK  Staging recetas v2")
    print(f"  URL: {url}")
    print(f"  Pestaña: {args.hoja}")
    print("\n  Manual: autorizar IMPORTRANGE en _AUX_MP y _AUX_SUB (una vez).")
    print("  Promover: python promover_staging_recetas.py [--dry-run] [--hoja ...]")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()

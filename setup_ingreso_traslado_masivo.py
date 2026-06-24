"""
Crea/actualiza en el libro staging las hojas de traslados masivos:

  INGRESO_TRASLADO   — bodega origen/destino + líneas (producto, cantidad)
  REGISTRO_TRASLADOS — historial permanente (una fila por línea, con TRX)
  CAT_TRASLADO       — catálogo oculto por bodega origen con lote referencial

Lote referencial:
  - Subreceta (SUB-xxx): rendimiento_estandar de BD_SUBRECETAS (ej. kimchi 5200 gr)
  - MP: par_level global si > 0; si no, vacío

Re-ejecutable. El botón ACEPTAR vive en Apps Script (tatami_staging.gs).

Uso:
  python setup_ingreso_traslado_masivo.py
"""

from __future__ import annotations

import logging
from collections import defaultdict

from dotenv import load_dotenv

from bodegas_config import BODEGAS, traslado_permitido
from staging_common import (
    batch_format,
    crear_hoja_si_no_existe,
    dropdown_list,
    dropdown_range,
    header_style,
    hide_sheet,
    open_master,
    find_header_row,
    sheets_api,
    staging_spreadsheet_id,
)

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHEET_INGRESO = "INGRESO_TRASLADO"
SHEET_REGISTRO = "REGISTRO_TRASLADOS"
SHEET_CAT = "CAT_TRASLADO"

FILA_LINEAS_INICIO = 6  # fila 5 = cabecera, fila 6 = primera línea
MAX_LINEAS = 50

REGISTRO_HEADERS = [
    "trx",
    "fecha_hora",
    "usuario",
    "bodega_origen",
    "bodega_destino",
    "producto",
    "cod_mp_sistema",
    "cantidad",
    "unidad_base",
    "lote_referencial",
    "cod_mov",
    "estado",
]


def _opciones_bodega() -> list[str]:
    out = []
    for cod, info in sorted(BODEGAS.items()):
        if info.activa:
            out.append(f"{cod} — {info.nombre}")
    return out


def _rendimientos_sub() -> dict[str, tuple[float, str]]:
    """cod SUB-036 → (rendimiento, unidad)."""
    from codigos_subreceta import cod_sub_canonico
    from subrecetas_detalle import cargar_bd_subrecetas

    out: dict[str, tuple[float, str]] = {}
    for cod, info in cargar_bd_subrecetas().items():
        try:
            rend = float(str(info.get("rendimiento_estandar") or "0").replace(",", "."))
        except (TypeError, ValueError):
            rend = 0.0
        un = str(info.get("unidad") or "gr").strip() or "gr"
        if rend > 0:
            out[cod_sub_canonico(cod)] = (rend, un)
    return out


def _par_por_mp(rows: list[dict]) -> dict[str, float]:
    """par_level global por cod_mp (primer valor > 0)."""
    out: dict[str, float] = {}
    for r in rows:
        cod = (r.get("cod_mp_sistema") or "").strip()
        if not cod:
            continue
        try:
            par = float(str(r.get("par_level") or "0").replace(",", "."))
        except (TypeError, ValueError):
            par = 0.0
        if par > 0 and cod not in out:
            out[cod] = par
    return out


def _lote_referencial(cod_mp: str, unidad_base: str, rend_sub: dict, par_mp: dict) -> str:
    cod = (cod_mp or "").strip().upper()
    if cod.startswith("SUB-"):
        hit = rend_sub.get(cod)
        if hit:
            rend, _un = hit
            return str(int(rend)) if rend == int(rend) else str(round(rend, 4))
        return ""
    par = par_mp.get(cod_mp) or par_mp.get(cod.replace("SUB-", ""))
    if par and par > 0:
        return str(int(par)) if par == int(par) else str(round(par, 4))
    return ""


def filas_catalogo() -> list[list[str]]:
    """
    [cod_bodega, etiqueta_dropdown, nombre_mp, cod_mp_sistema, lote_referencial, unidad_base]
    etiqueta: kimchi — SUB-036 — 5200 — gr

    Fuentes:
      - BD_MP_SISTEMA: todas las filas por bodega activa (MPs y SUB ya en inventario)
      - BD_SUBRECETAS: subrecetas activas faltantes en esa bodega (semis traslado)
    """
    from codigos_subreceta import cod_sub_canonico

    ws = open_master().worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_mp_sistema")
    if hi is None:
        return []
    h = [(c or "").strip() for c in vals[hi]]
    icod = h.index("cod_mp_sistema")
    inom = h.index("nombre_mp") if "nombre_mp" in h else None
    iub = h.index("unidad_base") if "unidad_base" in h else None
    ibod = h.index("cod_bodega") if "cod_bodega" in h else None
    if ibod is None:
        return []

    bodegas_cat = sorted(c for c, info in BODEGAS.items() if info.activa)
    rend_sub = _rendimientos_sub()
    subs_cab = {}
    try:
        from subrecetas_detalle import cargar_bd_subrecetas

        subs_cab = cargar_bd_subrecetas()
    except Exception as e:
        log.warning("BD_SUBRECETAS no cargada: %s", e)

    meta_mp: dict[str, dict] = {}
    por_bodega: dict[str, list[dict]] = defaultdict(list)

    for row in vals[hi + 1 :]:
        cod = (row[icod] if icod < len(row) else "").strip()
        bod = (row[ibod] if ibod < len(row) else "").strip().upper()
        if not cod or not bod or bod not in BODEGAS or not BODEGAS[bod].activa:
            continue
        nombre = (row[inom] if inom is not None and inom < len(row) else cod).strip() or cod
        ub = (row[iub] if iub is not None and iub < len(row) else "gr").strip() or "gr"
        cod_norm = cod_sub_canonico(cod) if str(cod).upper().startswith("SUB") else cod
        meta_mp[cod_norm] = {"nombre_mp": nombre, "unidad_base": ub, "cod_mp": cod_norm}
        por_bodega[bod].append(
            {"cod_bodega": bod, "cod_mp": cod_norm, "nombre_mp": nombre, "unidad_base": ub}
        )

    # Subrecetas activas en BD_SUBRECETAS → todas las bodegas de traslado físico
    bodegas_sub = [b for b in ("BOD-001", "BOD-002", "BOD-005") if b in bodegas_cat]
    for cod_raw, info in subs_cab.items():
        activa = str(info.get("activa") or "SI").strip().upper()
        if activa in ("NO", "FALSE", "0"):
            continue
        cod = cod_sub_canonico(cod_raw)
        nombre = (info.get("nombre_subreceta") or cod).strip() or cod
        ub = str(info.get("unidad") or "gr").strip() or "gr"
        if cod not in meta_mp:
            meta_mp[cod] = {"nombre_mp": nombre, "unidad_base": ub, "cod_mp": cod}
        for bod in bodegas_sub:
            ya = {r["cod_mp"].upper() for r in por_bodega[bod]}
            if cod.upper() not in ya:
                por_bodega[bod].append(
                    {
                        "cod_bodega": bod,
                        "cod_mp": cod,
                        "nombre_mp": meta_mp[cod]["nombre_mp"],
                        "unidad_base": meta_mp[cod]["unidad_base"],
                    }
                )

    par_mp: dict[str, float] = {}
    if "par_level" in h:
        ipar = h.index("par_level")
        for row in vals[hi + 1 :]:
            cod = (row[icod] if icod < len(row) else "").strip()
            if not cod:
                continue
            try:
                par = float(str(row[ipar] if ipar < len(row) else "0").replace(",", "."))
            except (TypeError, ValueError):
                par = 0.0
            if par > 0 and cod not in par_mp:
                par_mp[cod] = par

    vistos: set[tuple[str, str]] = set()
    filas: list[list[str]] = []
    for bod in bodegas_cat:
        for r in sorted(por_bodega.get(bod, []), key=lambda x: x["nombre_mp"].lower()):
            clave = (bod, r["cod_mp"].upper())
            if clave in vistos:
                continue
            vistos.add(clave)
            lote = _lote_referencial(r["cod_mp"], r["unidad_base"], rend_sub, par_mp)
            lote_txt = lote if lote else "—"
            etiqueta = f"{r['nombre_mp']} — {r['cod_mp']} — {lote_txt} — {r['unidad_base']}"
            filas.append([bod, etiqueta, r["nombre_mp"], r["cod_mp"], lote, r["unidad_base"]])
    return filas


def configurar_cat(sheets, sid: str) -> tuple[int, list[list[str]]]:
    sheet_id = crear_hoja_si_no_existe(sheets, sid, SHEET_CAT)
    filas = filas_catalogo()
    headers = [
        "cod_bodega",
        "etiqueta",
        "nombre_mp",
        "cod_mp_sistema",
        "lote_referencial",
        "unidad_base",
    ]
    sheets.spreadsheets().values().clear(spreadsheetId=sid, range=f"{SHEET_CAT}!A:F").execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_CAT}!A1",
        valueInputOption="RAW",
        body={"values": [headers] + filas},
    ).execute()
    # Columnas H+ = listas fijas por bodega (respaldo dropdown)
    por_bod: dict[str, list[str]] = defaultdict(list)
    for f in filas:
        por_bod[f[0]].append(f[1])
    col = 7  # H
    if por_bod:
        for i, bod in enumerate(sorted(por_bod.keys())):
            letter = chr(ord("H") + i)
            lista = [[bod]] + [[e] for e in por_bod[bod]]
            sheets.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"{SHEET_CAT}!{letter}1",
                valueInputOption="RAW",
                body={"values": lista},
            ).execute()
    hide_sheet(sheets, sid, sheet_id)
    por_bod_count: dict[str, int] = defaultdict(int)
    for f in filas:
        por_bod_count[f[0]] += 1
    log.info("CAT_TRASLADO: %d items %s", len(filas), dict(por_bod_count))
    return len(filas), filas


def configurar_ingreso(sheets, sid: str, n_cat: int, filas_cat: list[list[str]]) -> None:
    sheet_id = crear_hoja_si_no_existe(sheets, sid, SHEET_INGRESO)
    opciones_bod = _opciones_bodega()
    fin = FILA_LINEAS_INICIO + MAX_LINEAS - 1
    cat_end = max(n_cat + 1, 100)

    valores = [
        ["TRASLADO MASIVO ENTRE BODEGAS", "", "", "", ""],
        ["Bodega origen:", "", "", "", ""],
        ["Bodega destino:", "", "", "", ""],
        ["Complete origen (B2), destino (B3) y productos (col A) en cualquier orden.", "", "", "", ""],
        ["PRODUCTO", "CANTIDAD", "UNIDAD", "LOTE REF", "STOCK ORIGEN"],
    ]
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!A1",
        valueInputOption="RAW",
        body={"values": valores},
    ).execute()

    # F1 = código bodega origen
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!F1",
        valueInputOption="USER_ENTERED",
        body={"values": [['=SI($B$2="";"";REGEXEXTRACT($B$2;"BOD-[0-9]{3}"))']]},
    ).execute()

    # Lista completa en H (dropdown siempre tiene opciones; Apps Script filtra por bodega)
    vistos_h: set[str] = set()
    lista_h: list[list[str]] = []
    for f in filas_cat:
        et = f[1]
        if et and et not in vistos_h:
            vistos_h.add(et)
            lista_h.append([et])
    if lista_h:
        sheets.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"{SHEET_INGRESO}!H2",
            valueInputOption="RAW",
            body={"values": lista_h},
        ).execute()
        log.info("Lista H: %d productos unicos para dropdown", len(lista_h))

    r0, r1 = FILA_LINEAS_INICIO, fin
    cat_rng = f"{SHEET_CAT}!$B$2:$F${cat_end}"

    # Limpiar C:D (evita #REF! si ARRAYFORMULA choca con celdas con datos)
    sheets.spreadsheets().values().clear(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!C{r0}:D{r1}",
    ).execute()

    # Una fórmula por fila (sin ARRAYFORMULA — más estable en Sheets)
    filas_c: list[list[str]] = []
    filas_d: list[list[str]] = []
    for row in range(r0, r1 + 1):
        filas_c.append(
            [f'=SI(A{row}="";"";SI.ERROR(BUSCARV(A{row};{cat_rng};5;FALSO);""))']
        )
        filas_d.append(
            [f'=SI(A{row}="";"";SI.ERROR(BUSCARV(A{row};{cat_rng};4;FALSO);""))']
        )
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!C{r0}",
        valueInputOption="USER_ENTERED",
        body={"values": filas_c},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!D{r0}",
        valueInputOption="USER_ENTERED",
        body={"values": filas_d},
    ).execute()

    reqs: list[dict] = []
    for r0, r1, c0, c1 in [(0, 1, 0, 5), (4, 5, 0, 5)]:
        reqs.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": r0,
                        "endRowIndex": r1,
                        "startColumnIndex": c0,
                        "endColumnIndex": c1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.13, "green": 0.13, "blue": 0.13},
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            }
        )

    for row_idx, col_idx in [(1, 1), (2, 1)]:
        dd = dropdown_list(sheet_id, 0, opciones_bod, strict=True)
        dd["setDataValidation"]["range"] = {
            "sheetId": sheet_id,
            "startRowIndex": row_idx,
            "endRowIndex": row_idx + 1,
            "startColumnIndex": col_idx,
            "endColumnIndex": col_idx + 1,
        }
        reqs.append(dd)

    # Dropdown productos: lista completa en H, validacion NO estricta (cualquier orden de llenado)
    dd_prod = dropdown_range(sheet_id, 0, f"{SHEET_INGRESO}!$H$2:$H$1500")
    dd_prod["setDataValidation"]["rule"]["strict"] = False
    dd_prod["setDataValidation"]["range"] = {
        "sheetId": sheet_id,
        "startRowIndex": FILA_LINEAS_INICIO - 1,
        "endRowIndex": fin,
        "startColumnIndex": 0,
        "endColumnIndex": 1,
    }
    reqs.append(dd_prod)

    # Ocultar F:H helpers
    reqs.append(
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 5,
                    "endIndex": 8,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        }
    )
    reqs.append(
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 380},
                "fields": "pixelSize",
            }
        }
    )
    batch_format(sheets, sid, reqs)
    log.info("%s configurada (líneas %d-%d)", SHEET_INGRESO, FILA_LINEAS_INICIO, fin)


def configurar_registro(sheets, sid: str) -> None:
    sheet_id = crear_hoja_si_no_existe(sheets, sid, SHEET_REGISTRO)
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_REGISTRO}!A1",
        valueInputOption="RAW",
        body={"values": [REGISTRO_HEADERS]},
    ).execute()
    batch_format(sheets, sid, header_style(sheet_id, len(REGISTRO_HEADERS)))
    log.info("%s configurada (historial)", SHEET_REGISTRO)


def main() -> None:
    sheets = sheets_api()
    sid = staging_spreadsheet_id()
    n_cat, filas_cat = configurar_cat(sheets, sid)
    configurar_ingreso(sheets, sid, n_cat, filas_cat)
    configurar_registro(sheets, sid)

    pares = []
    for o in sorted(BODEGAS):
        for d in sorted(BODEGAS):
            if o != d and traslado_permitido(o, d):
                pares.append(f"{o}->{d}")

    print("\n" + "=" * 70)
    print("  OK  Traslados masivos")
    print(f"  Staging: https://docs.google.com/spreadsheets/d/{sid}")
    print(f"  Pestañas: {SHEET_INGRESO} | {SHEET_REGISTRO} | {SHEET_CAT} (oculta)")
    print()
    print("  Catálogo con lote referencial:")
    print("    SUB: rendimiento_estandar (ej. kimchi - SUB-036 - 5200 - gr)")
    print("    MP:  par_level global si existe")
    print()
    print(f"  Pares permitidos ({len(pares)}): " + ", ".join(pares[:8]) + ("..." if len(pares) > 8 else ""))
    print()
    print("  Railway/.env:")
    print("    TRASLADO_SHEETS_INGEST_SECRET")
    print("    TRASLADO_SHEETS_EMAILS=correo1@...,correo2@...")
    print("  BD_CONFIG (opcional): emails_traslado_masivo")
    print()
    print("  Apps Script: menú 📦 Tatami Traslados en tatami_staging.gs")
    print("    TATAMI_TRASLADO_API_URL / TATAMI_TRASLADO_SECRET")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()

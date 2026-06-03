"""
Calcula costo teórico por lote estándar de cada subreceta y escribe en BD_SUBRECETAS.

Fuentes:
  - MP: costo_unitario_ref en BD_MP_SISTEMA por (cod_mp_sistema, cod_bodega)
  - Subreceta hijo: costo_unitario_estandar ya calculado (orden topológico)

Fórmulas por fila de BD_SUBRECETAS_DETALLE (lote estándar del padre):
  MP:     cantidad × costo_unitario_ref × (1 + merma_pct)
  Hijo:   cantidad × costo_unitario_estandar(hijo)   [misma unidad_base que el detalle]

Cabecera:
  costo_lote_estandar = suma líneas
  costo_unitario_estandar = costo_lote / rendimiento_estandar

Uso:
  python calcular_costo_subrecetas.py
  python calcular_costo_subrecetas.py --produccion
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

from bodegas_config import normalizar_cod_bodega
from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas,
    cargar_bd_subrecetas_detalle,
    es_linea_mp_detalle,
    es_linea_subreceta_hijo,
    orden_produccion,
)

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_CABECERA = "BD_SUBRECETAS"

COL_LOTE = "costo_lote_estandar"
COL_UNIT = "costo_unitario_estandar"
COL_FECHA = "costo_calc_at"


def _safe_float(v, default: float = 0.0) -> float:
    from numeros_sheets import parse_numero_sheets

    return parse_numero_sheets(v, default)


def _norm_mp(cod: str) -> str:
    s = (cod or "").strip()
    if not s:
        return ""
    n = s.lstrip("0")
    return n if n else "0"


def cargar_costos_mp(sh) -> dict[tuple[str, str], float]:
    """(cod_mp_norm, cod_bodega) -> USD/unidad_base (canónico desde items prov)."""
    from costo_mp_canonico import cargar_costos_mp_para_recetas

    costos, avisos = cargar_costos_mp_para_recetas(sh)
    for a in avisos[:15]:
        print(f"  AVISO costo MP: {a}")
    if len(avisos) > 15:
        print(f"  ... y {len(avisos) - 15} avisos más")
    return costos


def _costo_mp(
    cod_mp: str,
    cod_bodega: str,
    costos: dict[tuple[str, str], float],
) -> tuple[float, str]:
    """Retorna (costo USD/unidad_base, nota_si_falta)."""
    from costo_mp_canonico import elegir_costo_mp

    return elegir_costo_mp(cod_mp, cod_bodega, costos)


def calcular_costos(
    cabeceras: dict[str, dict],
    por_padre: dict[str, list[dict]],
    costos_mp: dict[tuple[str, str], float],
) -> tuple[dict[str, dict], list[str]]:
    """
    Retorna cod -> {costo_lote, costo_unitario, rendimiento, unidad, warnings[]}
    """
    # Todas las subrecetas con detalle (activas o no), en orden hijo → padre
    cab_all = {c: cabeceras[c] for c in por_padre if c in cabeceras}
    orden = orden_produccion(cab_all, por_padre)
    restantes = sorted(set(por_padre) - set(orden))
    orden = orden + restantes
    resultados: dict[str, dict] = {}
    avisos: list[str] = []

    for cod in orden:
        cab = cabeceras.get(cod, {})
        rend = _safe_float(cab.get("rendimiento_estandar"))
        unidad = (cab.get("unidad") or "").strip()
        lineas = por_padre.get(cod, [])
        costo_lote = 0.0
        line_warnings: list[str] = []

        for ln in lineas:
            cant = _safe_float(ln.get("cantidad"))
            merma = _safe_float(ln.get("merma_pct"))
            bod = normalizar_cod_bodega(ln.get("cod_bodega"))

            if es_linea_subreceta_hijo(ln):
                hijo = (ln.get("cod_subreceta_hijo") or "").strip()
                info_h = resultados.get(hijo)
                if not info_h:
                    line_warnings.append(f"hijo_{hijo}_sin_costo_previo")
                    continue
                cu = info_h.get("costo_unitario", 0.0)
                if cu <= 0:
                    line_warnings.append(f"hijo_{hijo}_costo_cero")
                costo_lote += cant * cu

            elif es_linea_mp_detalle(ln):
                cmp_ = (ln.get("cod_mp_sistema") or "").strip()
                cu, nota = _costo_mp(cmp_, bod, costos_mp)
                if cu <= 0:
                    line_warnings.append(
                        f"mp_{cmp_}@{bod or '?'}_sin_costo"
                    )
                elif nota:
                    line_warnings.append(nota)
                costo_lote += cant * cu * (1.0 + merma)

        costo_unit = (costo_lote / rend) if rend > 0 else 0.0
        resultados[cod] = {
            "nombre": (cab.get("nombre_subreceta") or "").strip(),
            "costo_lote": round(costo_lote, 4),
            "costo_unitario": round(costo_unit, 6),
            "rendimiento": rend,
            "unidad": unidad,
        }
        if line_warnings:
            avisos.append(f"{cod} ({cab.get('nombre_subreceta', '')}): {', '.join(line_warnings[:5])}")

    return resultados, avisos


def _norm_sub_cod(cod: str) -> str:
    from codigos_subreceta import cod_sub_canonico

    return cod_sub_canonico(cod)


def resumen_subreceta_costo(
    cod: str,
    cab: dict,
    lineas: list[dict],
    costos_mp: dict[tuple[str, str], float],
    resultados_previos: dict[str, dict],
) -> dict:
    """Resumen + desglose por línea del lote estándar de una subreceta."""
    rend = _safe_float(cab.get("rendimiento_estandar"))
    unidad = (cab.get("unidad") or "").strip()
    nombre = (cab.get("nombre_subreceta") or "").strip()
    detalle: list[dict] = []
    costo_lote = 0.0
    notas: list[str] = []

    for ln in lineas:
        cant = _safe_float(ln.get("cantidad"))
        if cant <= 0:
            continue
        merma = _safe_float(ln.get("merma_pct"))
        bod = normalizar_cod_bodega(ln.get("cod_bodega"))
        unidad_ln = (ln.get("unidad_base") or unidad or "").strip()

        if es_linea_subreceta_hijo(ln):
            hijo = (ln.get("cod_subreceta_hijo") or "").strip()
            info_h = resultados_previos.get(hijo) or resultados_previos.get(
                _norm_sub_cod(hijo)
            )
            cu = float((info_h or {}).get("costo_unitario") or 0)
            line_cost = cant * cu
            nota = ""
            if cu <= 0:
                nota = f"hijo_{_norm_sub_cod(hijo) or hijo}_sin_costo"
            detalle.append(
                {
                    "tipo": "SUB",
                    "cod": hijo,
                    "nombre": (ln.get("nombre_subreceta_hijo") or "").strip(),
                    "cantidad": cant,
                    "unidad_base": unidad_ln,
                    "cod_bodega": "",
                    "merma_pct": merma,
                    "costo_unitario": round(cu, 6),
                    "costo_linea": round(line_cost, 4),
                    "nota": nota,
                }
            )
            costo_lote += line_cost
            if nota:
                notas.append(nota)

        elif es_linea_mp_detalle(ln):
            cmp_ = (ln.get("cod_mp_sistema") or "").strip()
            cu, nota_mp = _costo_mp(cmp_, bod, costos_mp)
            line_cost = cant * cu * (1.0 + merma)
            nota = nota_mp or ("" if line_cost > 0 else f"mp_{cmp_}_sin_costo")
            detalle.append(
                {
                    "tipo": "MP",
                    "cod": cmp_,
                    "nombre": (ln.get("nombre_mp") or "").strip(),
                    "cantidad": cant,
                    "unidad_base": unidad_ln,
                    "cod_bodega": bod,
                    "merma_pct": merma,
                    "costo_unitario": round(cu, 6),
                    "costo_linea": round(line_cost, 4),
                    "nota": nota,
                }
            )
            costo_lote += line_cost
            if nota:
                notas.append(nota)

    costo_unit = (costo_lote / rend) if rend > 0 else 0.0
    return {
        "cod_subreceta": cod.strip(),
        "nombre_subreceta": nombre,
        "rendimiento_estandar": rend,
        "unidad": unidad,
        "costo_lote_estandar_usd": round(costo_lote, 4),
        "costo_unitario_estandar_usd": round(costo_unit, 6),
        "detalle_lineas": sorted(detalle, key=lambda x: x.get("costo_linea", 0), reverse=True),
        "notas": ", ".join(dict.fromkeys(notas))[:500],
    }


def cargar_contexto_subrecetas(sh=None):
    """(cabeceras, por_padre, costos_mp, resultados_calc)."""
    import gspread
    from google.oauth2.service_account import Credentials

    if sh is None:
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
        )
        sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    cab = cargar_bd_subrecetas(sh)
    detalle = cargar_bd_subrecetas_detalle(sh)
    por_padre = agrupar_detalle_por_padre(detalle)
    costos_mp = cargar_costos_mp(sh)
    resultados, _ = calcular_costos(cab, por_padre, costos_mp)
    return cab, por_padre, costos_mp, resultados


def _asegurar_columnas_costo(ws, headers: list[str]) -> dict[str, int]:
    """Añade columnas de costo al header si faltan. Retorna índices 0-based."""
    nuevos = []
    h = list(headers)
    for col in (COL_LOTE, COL_UNIT, COL_FECHA):
        if col not in h:
            # insertar después de notas (col 5) o al final de datos
            h.append(col)
            nuevos.append(col)
    if nuevos:
        end = chr(ord("A") + len(h) - 1)
        ws.update(
            values=[h],
            range_name=f"A1:{end}1",
            value_input_option=ValueInputOption.user_entered,
        )
    h = [(c or "").strip() for c in ws.row_values(1)]

    def _primera(col: str) -> int:
        for i, name in enumerate(h):
            if name == col:
                return i
        raise ValueError(f"Columna {col} no encontrada en {SHEET_CABECERA}")

    return {COL_LOTE: _primera(COL_LOTE), COL_UNIT: _primera(COL_UNIT), COL_FECHA: _primera(COL_FECHA)}


def _aplicar_formato_costos(
    ws,
    *,
    start_row: int,
    end_row: int,
    col_lote: int,
    col_unit: int,
) -> None:
    """Formato numérico (evita que 8.54 se guarde como 8541 con locale es)."""
    sheet_id = ws.id
    body = {
        "requests": [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": col_lote,
                        "endColumnIndex": col_lote + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": col_unit,
                        "endColumnIndex": col_unit + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "NUMBER",
                                "pattern": "#,##0.000000",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
        ]
    }
    ws.spreadsheet.batch_update(body)


def escribir_costos(
    sh,
    cabeceras: dict[str, dict],
    resultados: dict[str, dict],
    produccion: bool,
) -> None:
    ws = sh.worksheet(SHEET_CABECERA)
    values = ws.get_all_values()
    hi = next(i for i, r in enumerate(values) if "cod_subreceta" in (r or []))
    headers = [(c or "").strip() for c in values[hi]]
    icod = headers.index("cod_subreceta")

    if produccion:
        col_idx = _asegurar_columnas_costo(ws, headers)
        headers = [(c or "").strip() for c in ws.row_values(1)]
        col_idx = {
            COL_LOTE: headers.index(COL_LOTE),
            COL_UNIT: headers.index(COL_UNIT),
            COL_FECHA: headers.index(COL_FECHA),
        }
    else:
        if COL_LOTE not in headers:
            print("  [DRY RUN] Columnas de costo aún no en hoja; se crearían al --produccion")
            col_idx = {
                COL_LOTE: len(headers),
                COL_UNIT: len(headers) + 1,
                COL_FECHA: len(headers) + 2,
            }
        else:
            col_idx = {
                c: next(i for i, h in enumerate(headers) if h == c)
                for c in (COL_LOTE, COL_UNIT, COL_FECHA)
            }

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    filas: list[tuple[int, str, float, float]] = []
    sin_costo = 0

    for i, row in enumerate(values[hi + 1 :], start=hi + 2):
        cod = (row[icod] if icod < len(row) else "").strip()
        if not cod:
            continue
        info = resultados.get(cod)
        if not info:
            continue
        if info["costo_lote"] <= 0:
            sin_costo += 1
        filas.append(
            (
                i,
                cod,
                float(info["costo_lote"]),
                float(info["costo_unitario"]),
            )
        )

    print(f"\nSubrecetas con costo > 0: {len(resultados) - sin_costo} / {len(resultados)}")
    if sin_costo:
        print(f"  Sin costo (revisar MPs): {sin_costo}")

    if not produccion:
        print("\n[DRY RUN] Top 10 por costo de lote:")
        top = sorted(resultados.items(), key=lambda x: x[1]["costo_lote"], reverse=True)[:10]
        for cod, info in top:
            print(
                f"  {cod} {info['nombre'][:30]:30} "
                f"lote={info['costo_lote']:.2f} "
                f"unit={info['costo_unitario']:.6f}/{info['unidad']}"
            )
        print("\nCorre con --produccion para escribir en BD_SUBRECETAS.")
        return

    if not filas:
        return

    ic_lote = col_idx[COL_LOTE]
    ic_unit = col_idx[COL_UNIT]
    ic_fecha = col_idx[COL_FECHA]
    start_row = min(f[0] for f in filas)
    end_row = max(f[0] for f in filas)

    num_updates: list[dict] = []
    fecha_updates: list[dict] = []
    for i, _cod, cl, cu in filas:
        num_updates.append(
            {"range": rowcol_to_a1(i, ic_lote + 1), "values": [[cl]]}
        )
        num_updates.append(
            {"range": rowcol_to_a1(i, ic_unit + 1), "values": [[cu]]}
        )
        fecha_updates.append(
            {"range": rowcol_to_a1(i, ic_fecha + 1), "values": [[ts]]}
        )

    for j in range(0, len(num_updates), 100):
        ws.batch_update(
            num_updates[j : j + 100],
            value_input_option=ValueInputOption.raw,
        )
        time.sleep(1.1)
    for j in range(0, len(fecha_updates), 100):
        ws.batch_update(
            fecha_updates[j : j + 100],
            value_input_option=ValueInputOption.user_entered,
        )
        time.sleep(1.1)

    _aplicar_formato_costos(
        ws,
        start_row=start_row,
        end_row=end_row,
        col_lote=ic_lote,
        col_unit=ic_unit,
    )
    print(
        f"  Escritos {len(filas)} costos (RAW numérico) en {SHEET_CABECERA} "
        f"filas {start_row}-{end_row}."
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Costo teórico subrecetas desde MPs")
    p.add_argument(
        "--produccion",
        action="store_true",
        help="Sin esto: dry run (no escribe en Sheets)",
    )
    args = p.parse_args()

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])

    print("Cargando maestros...")
    cab = cargar_bd_subrecetas(sh)
    detalle = cargar_bd_subrecetas_detalle(sh)
    por_padre = agrupar_detalle_por_padre(detalle)
    costos_mp = cargar_costos_mp(sh)
    print(f"  MPs con costo en maestro: {sum(1 for v in costos_mp.values() if v > 0)}")

    resultados, avisos = calcular_costos(cab, por_padre, costos_mp)

    if avisos:
        print(f"\nAvisos ({len(avisos)}):")
        for a in avisos[:25]:
            print(f"  - {a}")
        if len(avisos) > 25:
            print(f"  ... y {len(avisos) - 25} más")

    escribir_costos(sh, cab, resultados, args.produccion)


if __name__ == "__main__":
    main()

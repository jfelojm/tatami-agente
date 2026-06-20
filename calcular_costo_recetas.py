"""
Costo teórico por plato vendido (1 unidad) desde BD_RECETAS_DETALLE.

Por línea:
  MP:  cantidad × costo_unitario_ref × (1 + merma_pct) × pct_aplicacion
  SUB: cantidad × costo_unitario_estandar (BD_SUBRECETAS) × pct_aplicacion

Salida:
  - BD_RECETAS_DETALLE: costo_unitario, costo_linea, nota_costo por línea
  - BD_RECETAS: resumen por plato (costo_plato_estandar, lineas_sin_costo, …)

Uso:
  python calcular_costo_recetas.py
  python calcular_costo_recetas.py --produccion
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption

from bodegas_config import normalizar_cod_bodega
from calcular_costo_subrecetas import (
    _costo_mp,
    _safe_float,
    calcular_costos,
    cargar_costos_mp,
)
from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas,
    cargar_bd_subrecetas_detalle,
)
from recetas_detalle import (
    COLS_COSTO_RECETA_DETALLE,
    SHEET_DETALLE,
    SHEET_RESUMEN,
    agrupar_por_plato,
    cargar_bd_recetas_detalle,
    clave_plato,
    es_linea_mp,
    es_linea_subreceta,
    norm_cod_receta,
)
from google_credentials import google_credentials

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS_BD_RECETAS = [
    "nombre_receta",
    "cod_receta",
    "variedad_smart_menu",
    "n_lineas_mp",
    "n_lineas_sub",
    "costo_plato_estandar",
    "lineas_sin_costo",
    "notas_costo",
    "costo_calc_at",
]

COL_COSTO = "costo_plato_estandar"
COL_FECHA = "costo_calc_at"


def _col_letter(n: int) -> str:
    """1-based column index → A, B, … Z, AA."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _fila_dict_detalle(headers: list[str], row: list) -> dict:
    return {
        headers[k]: (row[k] if k < len(row) else "").strip()
        for k in range(len(headers))
        if headers[k]
    }


def _asegurar_columnas_costo_detalle(
    ws: gspread.Worksheet, headers: list[str], produccion: bool
) -> list[str]:
    missing = [c for c in COLS_COSTO_RECETA_DETALLE if c not in headers]
    if not missing:
        return headers
    if not produccion:
        print(f"  [DRY RUN] Añadiría columnas en {SHEET_DETALLE}: {missing}")
        return headers + missing
    new_headers = headers + missing
    end = _col_letter(len(new_headers))
    ws.update(
        values=[new_headers],
        range_name=f"A1:{end}1",
        value_input_option=ValueInputOption.user_entered,
    )
    print(f"  Columnas añadidas en {SHEET_DETALLE}: {missing}")
    return new_headers


def escribir_costos_bd_recetas_detalle(
    sh,
    costos_mp: dict[tuple[str, str], float],
    unitarios_sub: dict[str, float],
    produccion: bool,
) -> dict:
    """Escribe costo_unitario, costo_linea y nota_costo por fila ingrediente."""
    ws = sh.worksheet(SHEET_DETALLE)
    values = ws.get_all_values()
    hi = next(
        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_receta" for c in r)),
        None,
    )
    if hi is None:
        print(f"  WARN: {SHEET_DETALLE} sin cabecera cod_receta")
        return {"lineas": 0, "sin_costo": 0}

    headers = [(c or "").strip() for c in values[hi]]
    headers = _asegurar_columnas_costo_detalle(ws, headers, produccion)
    ic = {h: i for i, h in enumerate(headers)}
    for col in COLS_COSTO_RECETA_DETALLE:
        if col not in ic:
            ic[col] = len(headers)
            headers.append(col)

    i_cu = ic["costo_unitario"]
    i_cl = ic["costo_linea"]
    i_nota = ic["nota_costo"]
    col_cu = _col_letter(i_cu + 1)
    col_cl = _col_letter(i_cl + 1)
    col_nota = _col_letter(i_nota + 1)

    filas_cu: list[list] = []
    filas_cl: list[list] = []
    filas_nota: list[list] = []
    n_lineas = 0
    n_sin = 0
    ejemplos_sin: list[str] = []

    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            filas_cu.append([""])
            filas_cl.append([""])
            filas_nota.append([""])
            continue
        if str(row[0]).strip().startswith("["):
            filas_cu.append([""])
            filas_cl.append([""])
            filas_nota.append([""])
            continue
        ln = _fila_dict_detalle(headers, row)
        if not (ln.get("cod_receta") or "").strip():
            filas_cu.append([""])
            filas_cl.append([""])
            filas_nota.append([""])
            continue
        if not es_linea_mp(ln) and not es_linea_subreceta(ln):
            filas_cu.append([""])
            filas_cl.append([""])
            filas_nota.append([""])
            continue

        det = costo_linea_receta(ln, costos_mp, unitarios_sub)
        n_lineas += 1
        if not det:
            filas_cu.append([""])
            filas_cl.append([""])
            filas_nota.append([""])
            continue

        cu = det["costo_unitario"]
        cl = det["costo_linea"]
        nota = (det.get("nota") or "").strip()
        cod = det.get("cod", "?")
        if cl <= 0 or cu <= 0:
            n_sin += 1
            if not nota:
                tipo = det.get("tipo", "MP")
                nota = f"{tipo.lower()}_{cod}_sin_costo"
            if len(ejemplos_sin) < 12:
                nom = (det.get("nombre") or cod)[:35]
                ejemplos_sin.append(f"{det.get('tipo')} {cod} {nom} ({nota})")

        filas_cu.append([cu if cu > 0 else ""])
        filas_cl.append([cl if cl > 0 else ""])
        filas_nota.append([nota])

    print(f"\n{SHEET_DETALLE}: {n_lineas} líneas MP/SUB")
    print(f"  Sin costo (costo_linea=0): {n_sin}")
    if ejemplos_sin:
        print("  Ejemplos sin costo:")
        for e in ejemplos_sin:
            print(f"    - {e}")

    if not filas_cu:
        return {"lineas": n_lineas, "sin_costo": n_sin}

    last_row = hi + 1 + len(filas_cu)
    if not produccion:
        print(f"  [DRY RUN] Actualizaría {col_cu}2:{col_nota}{last_row}")
        return {"lineas": n_lineas, "sin_costo": n_sin}

    ws.update(
        values=filas_cu,
        range_name=f"{col_cu}{hi + 2}:{col_cu}{last_row}",
        value_input_option=ValueInputOption.user_entered,
    )
    ws.update(
        values=filas_cl,
        range_name=f"{col_cl}{hi + 2}:{col_cl}{last_row}",
        value_input_option=ValueInputOption.user_entered,
    )
    ws.update(
        values=filas_nota,
        range_name=f"{col_nota}{hi + 2}:{col_nota}{last_row}",
        value_input_option=ValueInputOption.user_entered,
    )
    time.sleep(0.5)
    print(f"  Costos escritos en {SHEET_DETALLE} ({col_cu}–{col_nota}).")
    return {"lineas": n_lineas, "sin_costo": n_sin}


def _norm_sub(cod: str) -> str:
    from codigos_subreceta import cod_sub_canonico

    return cod_sub_canonico(cod)


def cargar_unitarios_subreceta(sh) -> dict[str, float]:
    """cod_subreceta -> USD por unidad_base (recalculado desde MPs, no celdas de costo en hoja)."""
    cab = cargar_bd_subrecetas(sh)
    detalle = cargar_bd_subrecetas_detalle(sh)
    por_padre = agrupar_detalle_por_padre(detalle)
    costos_mp = cargar_costos_mp(sh)
    resultados, _ = calcular_costos(cab, por_padre, costos_mp)
    out: dict[str, float] = {}
    for cod, info in resultados.items():
        cu = float(info.get("costo_unitario") or 0)
        if cu > 0:
            out[cod.strip()] = cu
            nk = _norm_sub(cod)
            if nk:
                out[nk] = cu
    return out


def _costo_subreceta(cod_sub: str, unitarios: dict[str, float]) -> tuple[float, str]:
    raw = (cod_sub or "").strip()
    if not raw:
        return 0.0, "sin_cod_sub"
    cu = unitarios.get(raw) or unitarios.get(_norm_sub(raw), 0.0)
    if cu <= 0:
        return 0.0, f"sub_{_norm_sub(raw) or raw}_sin_costo"
    return cu, ""


def _pct_aplicacion(row: dict) -> float:
    pct = _safe_float(row.get("pct_aplicacion"), 1.0)
    if pct <= 0:
        return 1.0
    # Hoja puede traer 100 = 100%
    if pct > 1.0:
        return pct / 100.0
    return pct


def costo_linea_receta(
    ln: dict,
    costos_mp: dict[tuple[str, str], float],
    unitarios_sub: dict[str, float],
) -> dict | None:
    """Detalle de una línea MP o SUB (cantidad <= 0 → None)."""
    cant = _safe_float(ln.get("cantidad"))
    pct = _pct_aplicacion(ln)
    if cant <= 0:
        return None

    if es_linea_subreceta(ln):
        cod_s = (ln.get("cod_subreceta") or "").strip()
        cu, nota = _costo_subreceta(cod_s, unitarios_sub)
        line_cost = cant * cu * pct
        return {
            "tipo": "SUB",
            "cod": cod_s,
            "nombre": (ln.get("nombre_subreceta") or "").strip(),
            "cantidad": cant,
            "unidad_base": (ln.get("unidad_base") or "").strip(),
            "cod_bodega": "",
            "merma_pct": _safe_float(ln.get("merma_pct")),
            "pct_aplicacion": pct,
            "costo_unitario": round(cu, 6),
            "costo_linea": round(line_cost, 4),
            "nota": nota,
        }

    if es_linea_mp(ln):
        cmp_ = (ln.get("cod_mp_sistema") or "").strip()
        bod = normalizar_cod_bodega(ln.get("cod_bodega"))
        merma = _safe_float(ln.get("merma_pct"))
        cu, nota = _costo_mp(cmp_, bod, costos_mp)
        line_cost = cant * cu * (1.0 + merma) * pct
        return {
            "tipo": "MP",
            "cod": cmp_,
            "nombre": (ln.get("nombre_mp") or "").strip(),
            "cantidad": cant,
            "unidad_base": (ln.get("unidad_base") or "").strip(),
            "cod_bodega": bod,
            "merma_pct": merma,
            "pct_aplicacion": pct,
            "costo_unitario": round(cu, 6),
            "costo_linea": round(line_cost, 4),
            "nota": nota or ("" if line_cost > 0 else f"mp_{cmp_}_sin_costo"),
        }
    return None


def resumen_plato_costo(
    lineas: list[dict],
    costos_mp: dict[tuple[str, str], float],
    unitarios_sub: dict[str, float],
) -> dict:
    """Resumen + desglose por línea de un plato."""
    if not lineas:
        return {}
    ln0 = lineas[0]
    cod = norm_cod_receta(ln0.get("cod_receta") or "")
    var = (ln0.get("variedad_smart_menu") or "").strip()
    nombre = (ln0.get("nombre_receta") or "").strip()
    detalle: list[dict] = []
    costo = 0.0
    n_mp = 0
    n_sub = 0
    sin_costo = 0
    notas: list[str] = []

    for ln in lineas:
        d = costo_linea_receta(ln, costos_mp, unitarios_sub)
        if not d:
            continue
        detalle.append(d)
        costo += d["costo_linea"]
        if d["tipo"] == "MP":
            n_mp += 1
        else:
            n_sub += 1
        if d["costo_linea"] <= 0 and d.get("nota"):
            sin_costo += 1
            notas.append(d["nota"])
        elif d.get("nota") and d["costo_linea"] > 0:
            notas.append(d["nota"])

    notas_str = ", ".join(dict.fromkeys(notas))[:500]
    if sin_costo and not notas_str:
        notas_str = f"{sin_costo}_lineas_sin_costo"

    return {
        "clave": clave_plato(cod, var),
        "nombre_receta": nombre,
        "cod_receta": cod,
        "variedad_smart_menu": var,
        "n_lineas_mp": n_mp,
        "n_lineas_sub": n_sub,
        "costo_plato_estandar": round(costo, 4),
        "lineas_sin_costo": sin_costo,
        "notas_costo": notas_str,
        "detalle_lineas": detalle,
    }


_COSTOS_CTX_CACHE: tuple | None = None
_COSTOS_CTX_CACHE_AT: float = 0.0
_COSTOS_CTX_TTL_SEC = 300.0


def cargar_contexto_costos(sh=None, *, usar_cache: bool = True):
    """(costos_mp, unitarios_sub, por_plato, lineas_detalle)."""
    global _COSTOS_CTX_CACHE, _COSTOS_CTX_CACHE_AT
    import gspread
    import time

    now = time.monotonic()
    if (
        usar_cache
        and sh is None
        and _COSTOS_CTX_CACHE is not None
        and (now - _COSTOS_CTX_CACHE_AT) < _COSTOS_CTX_TTL_SEC
    ):
        return _COSTOS_CTX_CACHE

    if sh is None:
        creds = google_credentials(SCOPES)
        sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    detalle = cargar_bd_recetas_detalle(sh)
    ctx = (
        cargar_costos_mp(sh),
        cargar_unitarios_subreceta(sh),
        agrupar_por_plato(detalle),
        detalle,
    )
    if sh is not None or usar_cache:
        _COSTOS_CTX_CACHE = ctx
        _COSTOS_CTX_CACHE_AT = now
    return ctx


def calcular_costos_platos(
    por_plato: dict[str, list[dict]],
    costos_mp: dict[tuple[str, str], float],
    unitarios_sub: dict[str, float],
) -> tuple[list[dict], list[str]]:
    filas: list[dict] = []
    avisos: list[str] = []

    for key in sorted(por_plato.keys()):
        lineas = por_plato[key]
        if not lineas:
            continue
        res = resumen_plato_costo(lineas, costos_mp, unitarios_sub)
        filas.append({k: v for k, v in res.items() if k != "detalle_lineas"})
        if res.get("lineas_sin_costo"):
            avisos.append(
                f"{res['cod_receta']}|{res['variedad_smart_menu'] or '(base)'} "
                f"{res['nombre_receta'][:40]}: "
                f"{res['lineas_sin_costo']} líneas sin costo — "
                f"{res.get('notas_costo', '')[:120]}"
            )

    return filas, avisos


def _asegurar_hoja_bd_recetas(sh) -> gspread.Worksheet:
    try:
        return sh.worksheet(SHEET_RESUMEN)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_RESUMEN, rows=800, cols=len(HEADERS_BD_RECETAS))
        ws.update(
            values=[HEADERS_BD_RECETAS],
            range_name="A1",
            value_input_option=ValueInputOption.user_entered,
        )
        return ws


def escribir_bd_recetas(sh, filas: list[dict], produccion: bool) -> None:
    con_costo = sum(1 for f in filas if f["costo_plato_estandar"] > 0)
    print(f"\nPlatos en detalle: {len(filas)}")
    print(f"  Con costo > 0: {con_costo}")
    print(f"  Costo cero o incompleto: {len(filas) - con_costo}")

    if not produccion:
        print("\n[DRY RUN] Top 15 platos por costo:")
        top = sorted(filas, key=lambda x: x["costo_plato_estandar"], reverse=True)[:15]
        for f in top:
            print(
                f"  {f['cod_receta']:>4} {f['variedad_smart_menu'][:25]:25} "
                f"${f['costo_plato_estandar']:.2f}  "
                f"mp={f['n_lineas_mp']} sub={f['n_lineas_sub']} "
                f"sin={f['lineas_sin_costo']}"
            )
        print("\nCorre con --produccion para escribir en BD_RECETAS.")
        return

    ws = _asegurar_hoja_bd_recetas(sh)
    headers = [(c or "").strip() for c in ws.row_values(1)]
    if headers != HEADERS_BD_RECETAS:
        ws.update(
            values=[HEADERS_BD_RECETAS],
            range_name="A1",
            value_input_option=ValueInputOption.user_entered,
        )
        headers = HEADERS_BD_RECETAS

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ic = {h: i for i, h in enumerate(headers)}
    body: list[list] = []
    for f in filas:
        row = [""] * len(headers)
        row[ic["nombre_receta"]] = f["nombre_receta"]
        row[ic["cod_receta"]] = f["cod_receta"]
        row[ic["variedad_smart_menu"]] = f["variedad_smart_menu"]
        row[ic["n_lineas_mp"]] = f["n_lineas_mp"]
        row[ic["n_lineas_sub"]] = f["n_lineas_sub"]
        row[ic[COL_COSTO]] = f["costo_plato_estandar"]
        row[ic["lineas_sin_costo"]] = f["lineas_sin_costo"]
        row[ic["notas_costo"]] = f["notas_costo"]
        row[ic[COL_FECHA]] = ts
        body.append(row)

    if ws.row_count < len(body) + 1:
        ws.resize(rows=len(body) + 5, cols=len(headers))

    # Limpiar datos viejos
    if ws.row_count > 1:
        ws.batch_clear([f"A2:{chr(ord('A') + len(headers) - 1)}{ws.row_count}"])

    if not body:
        print("  Sin filas para escribir.")
        return

    end_col = chr(ord("A") + len(headers) - 1)
    # RAW + float: Sheets es-EC no reinterpreta 20.41 como veinte mil.
    ws.update(
        values=body,
        range_name=f"A2:{end_col}{len(body) + 1}",
        value_input_option=ValueInputOption.user_entered,
    )
    time.sleep(1.0)

    # Fecha como texto legible (columna aparte ya en body con RAW — ok ISO)
    ic_costo = ic[COL_COSTO]
    sheet_id = ws.id
    sh.batch_update(
        {
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": len(body) + 1,
                            "startColumnIndex": ic_costo,
                            "endColumnIndex": ic_costo + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            ]
        }
    )
    print(f"  Escritas {len(body)} filas en {SHEET_RESUMEN}.")


def main() -> None:
    p = argparse.ArgumentParser(description="Costo teórico por plato (MP + subrecetas)")
    p.add_argument(
        "--produccion",
        action="store_true",
        help="Sin esto: dry run (no escribe en Sheets)",
    )
    args = p.parse_args()

    creds = google_credentials(SCOPES)
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])

    print("Cargando maestros...")
    detalle = cargar_bd_recetas_detalle(sh)
    por_plato = agrupar_por_plato(detalle)
    costos_mp = cargar_costos_mp(sh)
    unitarios_sub = cargar_unitarios_subreceta(sh)
    n_sub_lines = sum(1 for ln in detalle if es_linea_subreceta(ln))
    n_mp_lines = sum(1 for ln in detalle if es_linea_mp(ln))
    print(f"  Líneas detalle: {len(detalle)} (MP {n_mp_lines}, SUB {n_sub_lines})")
    print(f"  Platos únicos: {len(por_plato)}")
    print(f"  Subrecetas valoradas: {len(unitarios_sub)}")

    filas, avisos = calcular_costos_platos(por_plato, costos_mp, unitarios_sub)

    if avisos:
        print(f"\nAvisos platos ({len(avisos)}):")
        for a in avisos[:30]:
            print(f"  - {a}")
        if len(avisos) > 30:
            print(f"  ... y {len(avisos) - 30} más")

    escribir_costos_bd_recetas_detalle(sh, costos_mp, unitarios_sub, args.produccion)
    escribir_bd_recetas(sh, filas, args.produccion)


if __name__ == "__main__":
    main()

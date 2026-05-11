"""
Reconciliación: totales del grid Smart Menu vs hist_ventas en Supabase (misma fecha).

Sale con código 1 si no cuadra (para cortar pipeline antes de descargo de inventario).

Uso:
  python reconciliar_ventas_dia.py --fecha 2026-05-09
  python reconciliar_ventas_dia.py   # default: hoy

Variables:
  RECONCILIAR_TOL_ABS   tolerancia USD en subtotal (default 0.05)
  SUPABASE_URL / SUPABASE_KEY

Salida: imprime cuadre y alerta (alertas_tatami) si falla.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv(override=True)


def _tol() -> float:
    raw = os.getenv("RECONCILIAR_TOL_ABS", "0.05").strip().replace(",", ".")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.05


def _count_grid_rows(fecha: str) -> int:
    from ventas_smartmenu import descargar_ventas_grid

    rows = descargar_ventas_grid(fecha)
    return len(rows)


def _count_distinct_docs_hist(fecha: str) -> int:
    from ventas_smartmenu import supabase

    docs: set[str] = set()
    offset = 0
    while True:
        chunk = (
            supabase.table("hist_ventas")
            .select("num_documento")
            .eq("fecha", fecha)
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        for row in chunk:
            nd = (row.get("num_documento") or "").strip()
            if nd:
                docs.add(nd)
        if len(chunk) < 1000:
            break
        offset += 1000
    return len(docs)


def reconciliar(fecha: str, tol_abs: float) -> tuple[bool, dict]:
    from ventas_smartmenu import auditar_hist_ventas_dia
    from ventas_smartmenu_total import calcular_total_smartmenu

    fecha = (fecha or "").strip().split()[0]

    grid_sin = calcular_total_smartmenu(
        fecha, sin_iva=True, incluir_anulados=False
    )
    grid_iva = calcular_total_smartmenu(
        fecha, sin_iva=False, incluir_anulados=False
    )
    aud = auditar_hist_ventas_dia(fecha)

    columna_estado = aud.get("columna_estado_ok")
    sub_hist = aud["sum_subtotal_neto"] if columna_estado else aud["sum_subtotal"]
    total_hist = aud["sum_total_neto"] if columna_estado else aud["sum_total"]
    desc_hist = aud["sum_desc_neto"] if columna_estado else aud["sum_desc"]

    diff_sub = abs(sub_hist - grid_sin["total"])
    internal_check = abs(total_hist - (sub_hist - desc_hist))

    n_grid = _count_grid_rows(fecha)
    n_hist_docs = _count_distinct_docs_hist(fecha)
    diff_docs = n_grid - n_hist_docs

    report: dict = {
        "fecha": fecha,
        "tol_abs": tol_abs,
        "grid_subtotal_sin_iva": grid_sin["total"],
        "grid_total_con_iva": grid_iva["total"],
        "grid_docs_activos": grid_sin["docs"],
        "grid_docs_anulados": grid_sin["docs_anulados"],
        "grid_filas_cabecera": n_grid,
        "hist_subtotal": sub_hist,
        "hist_total_lineas": total_hist,
        "hist_descuentos": desc_hist,
        "hist_lineas_tabla": aud["lineas"],
        "hist_docs_distinct": n_hist_docs,
        "diff_sub": diff_sub,
        "diff_docs": diff_docs,
        "internal_check": internal_check,
        "columna_estado_documento": columna_estado,
    }

    failures: list[str] = []
    warnings: list[str] = []

    if n_grid == 0 and aud["lineas"] == 0:
        report["ok"] = True
        report["motivo"] = "dia_sin_ventas"
        return True, report

    if aud["lineas"] == 0 and n_grid > 0:
        failures.append("hist_ventas vacio pero grid tiene cabeceras de documento")

    if diff_sub > tol_abs:
        failures.append(
            f"subtotal grid ({grid_sin['total']:.2f}) vs hist ({sub_hist:.2f}) "
            f"diff={diff_sub:.2f} > tol {tol_abs:.2f}"
        )

    if diff_docs != 0:
        failures.append(
            f"documentos grid={n_grid} vs distinct num_documento en hist={n_hist_docs}"
        )

    if internal_check > 0.02:
        failures.append(
            f"inconsistencia interna hist (total vs subtotal-desc): diff={internal_check:.4f}"
        )

    if not columna_estado:
        warnings.append(
            "sin columna estado_documento — ejecutar sql/add_hist_ventas_estado_documento.sql "
            "para cuadre correcto con anulados"
        )

    ok = len(failures) == 0
    report["ok"] = ok
    if failures:
        report["failures"] = failures
    if warnings:
        report["warnings"] = warnings

    return ok, report


def main() -> None:
    from alertas_tatami import enviar_alerta

    p = argparse.ArgumentParser(description="Reconciliar grid Smart Menu vs hist_ventas")
    p.add_argument("--fecha", default=None, help="YYYY-MM-DD (default: hoy local)")
    p.add_argument(
        "--tol",
        type=float,
        default=None,
        help="Override tolerancia USD (default env RECONCILIAR_TOL_ABS o 0.05)",
    )
    args = p.parse_args()

    fecha = args.fecha or date.today().isoformat()
    tol_abs = args.tol if args.tol is not None else _tol()

    print("=" * 55)
    print(f"RECONCILIACION VENTAS — {fecha}")
    print(f"  tolerancia subtotal: {tol_abs:.2f} USD")
    print("=" * 55)

    ok, rep = reconciliar(fecha, tol_abs)

    print(f"\n  Grid subtotal (sin IVA, docs no anulados): {rep['grid_subtotal_sin_iva']:.2f}")
    print(f"  Grid total (con IVA, mismos docs):        {rep['grid_total_con_iva']:.2f}")
    print(f"  hist subtotal (neto si hay estado_doc):   {rep['hist_subtotal']:.2f}")
    print(
        f"  hist total líneas / descuentos:           {rep['hist_total_lineas']:.2f} / {rep['hist_descuentos']:.2f}"
    )
    print(f"  Lineas en tabla hist_ventas:              {rep['hist_lineas_tabla']}")
    print(f"  Docs grid (cabeceras):                   {rep['grid_filas_cabecera']}")
    print(f"  Docs distinct hist (num_documento):       {rep['hist_docs_distinct']}")
    print(f"  Diff subtotal:                           {rep['diff_sub']:.4f}")
    print(f"  Diff docs (grid - hist):                  {rep['diff_docs']}")

    if rep.get("warnings"):
        for w in rep["warnings"]:
            print(f"\n  WARN: {w}")

    if ok:
        print("\n  RESULTADO: OK — cuadre dentro de tolerancia.")
        raise SystemExit(0)

    print("\n  RESULTADO: FALLO — no ejecutar descargo hasta corregir.")
    if rep.get("failures"):
        for f in rep["failures"]:
            print(f"    - {f}")

    detalle = json.dumps(
        {k: v for k, v in rep.items() if k != "ok"},
        ensure_ascii=False,
        indent=2,
    )
    enviar_alerta(
        "Reconciliación ventas fallida (grid vs hist_ventas)",
        detalle,
        estado="ERROR",
    )
    try:
        from alertas_tatami import alerta_wa_reconciliacion_fallo

        alerta_wa_reconciliacion_fallo(rep)
    except Exception as e:
        print(f"  WARN: no se pudo enviar alerta WhatsApp: {e}")
    raise SystemExit(1)


if __name__ == "__main__":
    main()

"""
Reconciliación: totales del grid Smart Menu vs hist_ventas en Supabase (misma fecha).

Sale con código 1 si no cuadra con grid accesible (corta pipeline antes de descargo).
Si Smart Menu no responde (red/apagado), sale 0 con WARN — no bloquea el pipeline.

Uso:
  python reconciliar_ventas_dia.py --fecha 2026-05-09
  python reconciliar_ventas_dia.py   # default: hoy

Variables:
  RECONCILIAR_TOL_ABS   tolerancia USD en subtotal (default 0.05)
  SUPABASE_URL / SUPABASE_KEY

Salida: imprime cuadre y alerta (alertas_tatami) si falla el cuadre real.
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


def _count_distinct_docs_hist(fecha: str) -> int:
    from ventas_completitud import id_documentos_hist
    from ventas_smartmenu import supabase

    return len(id_documentos_hist(supabase, fecha))


def reconciliar(fecha: str, tol_abs: float) -> tuple[bool, dict]:
    from ventas_smartmenu import auditar_hist_ventas_dia, descargar_ventas_grid_con_meta
    from ventas_smartmenu_total import calcular_total_smartmenu

    fecha = (fecha or "").strip().split()[0]

    grid_fetch = descargar_ventas_grid_con_meta(fecha)
    grid_rows = grid_fetch.rows
    aud = auditar_hist_ventas_dia(fecha)

    columna_estado = aud.get("columna_estado_ok")
    sub_hist = aud["sum_subtotal_neto"] if columna_estado else aud["sum_subtotal"]
    total_hist = aud["sum_total_neto"] if columna_estado else aud["sum_total"]
    desc_hist = aud["sum_desc_neto"] if columna_estado else aud["sum_desc"]
    hist_neto = sub_hist - desc_hist

    if not grid_fetch.disponible:
        report: dict = {
            "fecha": fecha,
            "tol_abs": tol_abs,
            "smart_menu_disponible": False,
            "smart_menu_motivo": grid_fetch.motivo,
            "grid_subtotal_sin_iva": 0.0,
            "grid_ventas_brutas": 0.0,
            "grid_descuentos": 0.0,
            "grid_total_con_iva": 0.0,
            "hist_ventas_netas": hist_neto,
            "grid_docs_activos": 0,
            "grid_docs_anulados": 0,
            "grid_filas_cabecera": 0,
            "hist_subtotal": sub_hist,
            "hist_total_lineas": total_hist,
            "hist_descuentos": desc_hist,
            "hist_lineas_tabla": aud["lineas"],
            "hist_docs_distinct": _count_distinct_docs_hist(fecha),
            "diff_sub": 0.0,
            "diff_docs": 0,
            "internal_check": abs(total_hist - hist_neto),
            "columna_estado_documento": columna_estado,
            "ok": True,
            "motivo": "smart_menu_no_disponible",
            "warnings": [
                "Smart Menu no accesible — cuadre grid vs hist omitido "
                f"({grid_fetch.motivo}); pipeline continúa con hist_ventas existente"
            ],
        }
        return True, report

    grid_sin = calcular_total_smartmenu(
        fecha, sin_iva=True, incluir_anulados=False, rows=grid_rows
    )
    grid_iva = calcular_total_smartmenu(
        fecha, sin_iva=False, incluir_anulados=False, rows=grid_rows
    )

    # grid_sin["total"] = ventas netas (brutas − descuento documento en col 16)
    diff_sub = abs(hist_neto - grid_sin["total"])
    internal_check = abs(total_hist - hist_neto)

    n_grid = len(grid_rows)
    n_hist_docs = _count_distinct_docs_hist(fecha)
    diff_docs = n_grid - n_hist_docs

    report = {
        "fecha": fecha,
        "tol_abs": tol_abs,
        "smart_menu_disponible": True,
        "smart_menu_motivo": grid_fetch.motivo,
        "grid_subtotal_sin_iva": grid_sin["total"],
        "grid_ventas_brutas": grid_sin.get("total_bruto", grid_sin["total"]),
        "grid_descuentos": grid_sin.get("total_descuentos", 0.0),
        "grid_total_con_iva": grid_iva["total"],
        "hist_ventas_netas": hist_neto,
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
            f"ventas netas grid ({grid_sin['total']:.2f}) vs hist neto ({hist_neto:.2f}) "
            f"diff={diff_sub:.2f} > tol {tol_abs:.2f}"
        )

    if diff_docs != 0:
        failures.append(
            f"documentos grid={n_grid} vs id_documento en hist={n_hist_docs}"
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

    print(
        f"\n  Grid ventas brutas (sin IVA):             {rep.get('grid_ventas_brutas', rep['grid_subtotal_sin_iva']):.2f}"
    )
    print(f"  Grid descuentos (documento):              {rep.get('grid_descuentos', 0):.2f}")
    print(f"  Grid ventas netas (oficial):               {rep['grid_subtotal_sin_iva']:.2f}")
    print(f"  Grid total (con IVA, mismos docs):        {rep['grid_total_con_iva']:.2f}")
    print(f"  hist subtotal (bruto líneas):             {rep['hist_subtotal']:.2f}")
    print(f"  hist descuentos (línea):                  {rep['hist_descuentos']:.2f}")
    print(f"  hist ventas netas (subtotal − desc):      {rep.get('hist_ventas_netas', 0):.2f}")
    print(f"  hist total (campo total línea):          {rep['hist_total_lineas']:.2f}")
    print(f"  Lineas en tabla hist_ventas:              {rep['hist_lineas_tabla']}")
    print(f"  Docs grid (cabeceras):                   {rep['grid_filas_cabecera']}")
    print(f"  Docs distinct hist (id_documento):        {rep['hist_docs_distinct']}")
    print(f"  Diff subtotal:                           {rep['diff_sub']:.4f}")
    print(f"  Diff docs (grid - hist):                  {rep['diff_docs']}")

    if rep.get("warnings"):
        for w in rep["warnings"]:
            print(f"\n  WARN: {w}")

    if rep.get("motivo") == "smart_menu_no_disponible":
        print(
            "\n  RESULTADO: OMITIDO — Smart Menu no accesible; cuadre pospuesto "
            "(no es fallo de datos)."
        )
        detalle = json.dumps(
            {k: v for k, v in rep.items() if k != "ok"},
            ensure_ascii=False,
            indent=2,
        )
        enviar_alerta(
            "Reconciliación omitida — Smart Menu no accesible",
            detalle,
            estado="WARN",
        )
        try:
            from alertas_tatami import alerta_wa_smart_menu_no_disponible

            alerta_wa_smart_menu_no_disponible(rep)
        except Exception as e:
            print(f"  WARN: no se pudo enviar alerta WhatsApp: {e}")
        raise SystemExit(0)

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

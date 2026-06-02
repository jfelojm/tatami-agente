"""
Repoblar ventas Smart Menu -> hist_ventas desde una fecha (días con huecos).

Uso:
  python repoblar_ventas_desde.py --desde 2026-05-29
  python repoblar_ventas_desde.py --desde 2026-05-29 --descargo
  python repoblar_ventas_desde.py --desde 2026-05-29 --hasta 2026-06-01 --descargo

Por defecto solo inserta faltantes en hist_ventas (no --reemplazar).
Con --descargo: tras cuadrar ventas, descarga pendientes del día (sin --rehacer).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent


def _daterange(desde: date, hasta: date) -> list[date]:
    out: list[date] = []
    d = desde
    while d <= hasta:
        out.append(d)
        d += timedelta(days=1)
    return out


def _auditar(fecha: str) -> dict:
    from ventas_completitud import auditar_completitud, id_documentos_desde_grid_rows, mensaje_completitud
    from ventas_smartmenu import descargar_ventas_grid, supabase

    rows = descargar_ventas_grid(fecha)
    if not rows:
        return {"fecha": fecha, "ok": True, "grid_docs": 0, "hist_docs": 0, "sin_ventas": True}
    grid_ids = id_documentos_desde_grid_rows(rows)
    rep = auditar_completitud(fecha, grid_ids, sb=supabase)
    rep["mensaje"] = mensaje_completitud(rep)
    rep["sin_ventas"] = False
    return rep


def main() -> None:
    p = argparse.ArgumentParser(description="Repoblar ventas faltantes desde una fecha")
    p.add_argument("--desde", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--hasta", default=None, help="YYYY-MM-DD (default: ayer EC)")
    p.add_argument(
        "--descargo",
        action="store_true",
        help="Tras ventas OK, descargo pendientes del día (sin rehacer movimientos)",
    )
    args = p.parse_args()

    desde = datetime.strptime(args.desde.strip()[:10], "%Y-%m-%d").date()
    if args.hasta:
        hasta = datetime.strptime(args.hasta.strip()[:10], "%Y-%m-%d").date()
    else:
        from ventas_smartmenu import _fecha_hoy_ec

        hasta = datetime.strptime(_fecha_hoy_ec(), "%Y-%m-%d").date() - timedelta(days=1)

    if hasta < desde:
        desde, hasta = hasta, desde

    py = sys.executable
    print("=" * 55)
    print(f"REPOBLAR VENTAS {desde} .. {hasta}")
    print("=" * 55)

    for d in _daterange(desde, hasta):
        f = d.isoformat()
        print(f"\n--- {f} ---")
        rep = _auditar(f)
        if rep.get("sin_ventas"):
            print("  Sin ventas en grid — omitido")
            continue
        if rep.get("ok"):
            print(f"  OK ({rep['grid_docs']} docs)")
            if args.descargo:
                print("  Descargo pendientes...")
                r = subprocess.run(
                    [py, str(ROOT / "descargo_inventario.py"), "--fecha", f],
                    cwd=str(ROOT),
                )
                if r.returncode != 0:
                    print(f"  WARN descargo exit {r.returncode}")
            continue

        print(f"  INCOMPLETO: {rep.get('mensaje', '')}")
        r = subprocess.run(
            [py, str(ROOT / "ventas_smartmenu.py"), "--fecha", f],
            cwd=str(ROOT),
        )
        if r.returncode != 0:
            print(f"  ERROR ventas exit {r.returncode} — revisar manualmente")
            continue

        rep2 = _auditar(f)
        if not rep2.get("ok"):
            print(f"  WARN sigue incompleto: {rep2.get('mensaje', '')}")
            continue
        print(f"  Reparado: {rep2['grid_docs']} docs")

        if args.descargo:
            print("  Descargo pendientes...")
            r = subprocess.run(
                [py, str(ROOT / "descargo_inventario.py"), "--fecha", f],
                cwd=str(ROOT),
            )
            if r.returncode != 0:
                print(f"  WARN descargo exit {r.returncode}")

    print("\nCompletado.")


if __name__ == "__main__":
    main()

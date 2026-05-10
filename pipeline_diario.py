"""
Pipeline diario: ventas -> descargo -> facturas -> recalcular stock -> PAR/consumo.

Uso (desde la carpeta tatami-agente, con venv activado o python del venv):
  python pipeline_diario.py
  python pipeline_diario.py --skip-ventas

Variables: PYTHONIOENCODING=utf-8 se fuerza en el entorno del proceso hijo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _env() -> dict:
    e = os.environ.copy()
    e.setdefault("PYTHONIOENCODING", "utf-8")
    return e


def run_step(name: str, argv: list[str], *, check: bool = True) -> int:
    print("\n" + "=" * 60)
    print(f"PIPELINE: {name}")
    print("=" * 60)
    cmd = [sys.executable, *argv]
    print(f"  $ {' '.join(argv)}\n")
    r = subprocess.run(cmd, cwd=str(ROOT), env=_env())
    if check and r.returncode != 0:
        print(f"\nERROR: paso fallo con codigo {r.returncode}")
        sys.exit(r.returncode)
    if not check and r.returncode != 0:
        print(f"\nWARN: paso termino con codigo {r.returncode} (se continua)")
    return r.returncode


def main() -> None:
    skip_ventas = "--skip-ventas" in sys.argv
    strict_ventas = "--strict-ventas" in sys.argv
    hoy = date.today().strftime("%Y-%m-%d")

    print("=" * 60)
    print("PIPELINE DIARIO - Tatami")
    print("=" * 60)
    print(f"  Directorio: {ROOT}")
    print(f"  Python:     {sys.executable}")
    print(f"  Fecha hoy:  {hoy}")
    print(f"  Skip ventas Smart Menu: {skip_ventas}")
    print(f"  Ventas estrictas (--strict-ventas): {strict_ventas}")

    if not skip_ventas:
        ventas_argv = ["ventas_smartmenu.py", "--fecha", hoy]
        if strict_ventas:
            ventas_argv.append("--strict")
        # Por defecto mejor esfuerzo; con --strict-ventas falla el pipeline si ventas falla.
        run_step(
            "1/5 — Ventas Smart Menu -> hist_ventas",
            ventas_argv,
            check=strict_ventas,
        )
    else:
        print("\n[1/5] Omitido (--skip-ventas)")

    run_step(
        "2/5 — Descargo inventario (hist_ventas -> mov_inventario + stock Sheets)",
        ["descargo_inventario.py"],
    )
    run_step(
        "3/5 — Procesar facturas Drive (mov + BD_ITEMS_PROV + BD_MP_SISTEMA)",
        ["procesar_facturas_drive.py"],
    )
    run_step(
        "4/5 — Recalcular stock/costo Sheets desde mov_inventario",
        ["recalcular_stock_sheets.py", "--produccion"],
    )
    run_step(
        "5/5 — Calcular PAR y consumo diario en BD_MP_SISTEMA",
        ["calcular_par_levels.py"],
    )

    print("\n" + "=" * 60)
    print("PIPELINE DIARIO COMPLETADO")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()

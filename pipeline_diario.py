"""
Pipeline diario: ventas -> reconciliacion -> descargo -> facturas -> recalcular stock -> PAR/consumo.

Uso (desde la carpeta tatami-agente, con venv activado o python del venv):
  python pipeline_diario.py
  python pipeline_diario.py --skip-ventas
  python pipeline_diario.py --skip-reconciliar
  python pipeline_diario.py --strict-ventas

Variables: PYTHONIOENCODING=utf-8 se fuerza en el entorno del proceso hijo.
Reconciliación: RECONCILIAR_TOL_ABS, TATAMI_ALERT_WEBHOOK_URL, TATAMI_ALERT_LOG_PATH
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
    skip_reconciliar = "--skip-reconciliar" in sys.argv
    strict_ventas = "--strict-ventas" in sys.argv
    hoy = date.today().strftime("%Y-%m-%d")

    print("=" * 60)
    print("PIPELINE DIARIO - Tatami")
    print("=" * 60)
    print(f"  Directorio: {ROOT}")
    print(f"  Python:     {sys.executable}")
    print(f"  Fecha hoy:  {hoy}")
    print(f"  Skip ventas Smart Menu: {skip_ventas}")
    print(f"  Skip reconciliacion grid vs hist: {skip_reconciliar}")
    print(f"  Ventas estrictas (--strict-ventas): {strict_ventas}")

    if not skip_ventas:
        ventas_argv = ["ventas_smartmenu.py", "--fecha", hoy]
        if strict_ventas:
            ventas_argv.append("--strict")
        rc = run_step(
            "1/6 — Ventas Smart Menu -> hist_ventas",
            ventas_argv,
            check=False,
        )
        if strict_ventas and rc != 0:
            try:
                from alertas_tatami import alerta_wa_ventas_strict_fallo, enviar_alerta

                enviar_alerta(
                    "Pipeline: ventas_smartmenu fallo (--strict-ventas)",
                    f"fecha={hoy} codigo_salida={rc}\n"
                    "Revisar log del Programador de tareas o ejecutar ventas a mano.",
                    estado="ERROR",
                )
                alerta_wa_ventas_strict_fallo(hoy, rc)
            except Exception as e:
                print(f"  WARN: no se pudo enviar alerta: {e}")
            print(f"\nERROR: ventas termino con codigo {rc} (--strict-ventas)")
            sys.exit(rc)

        if not skip_reconciliar:
            run_step(
                "2/6 — Reconciliar grid Smart Menu vs hist_ventas",
                ["reconciliar_ventas_dia.py", "--fecha", hoy],
                check=True,
            )
        else:
            print("\n[2/6] Omitido (--skip-reconciliar) — riesgo: descargo sin cuadre")
    else:
        print("\n[1/6] Omitido (--skip-ventas)")
        print("\n[2/6] Omitido (sin ventas nuevas)")

    run_step(
        "3/6 — Descargo inventario (hist_ventas -> mov_inventario + stock Sheets)",
        ["descargo_inventario.py"],
    )
    run_step(
        "4/6 — Procesar facturas Drive (mov + BD_ITEMS_PROV + BD_MP_SISTEMA)",
        ["procesar_facturas_drive.py"],
    )
    run_step(
        "5/6 — Recalcular stock/costo Sheets desde mov_inventario",
        ["recalcular_stock_sheets.py", "--produccion"],
    )
    run_step(
        "6/6 — Calcular PAR y consumo diario en BD_MP_SISTEMA",
        ["calcular_par_levels.py"],
    )

    print("\n" + "=" * 60)
    print("PIPELINE DIARIO COMPLETADO")
    print("=" * 60 + "\n")

    try:
        from alertas_tatami import alerta_wa_pipeline_ok

        alerta_wa_pipeline_ok(hoy)
    except Exception as e:
        print(f"  WARN: notificacion WA pipeline OK: {e}")


if __name__ == "__main__":
    main()

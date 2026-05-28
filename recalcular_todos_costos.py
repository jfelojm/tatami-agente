"""
Cadena completa de costos teóricos (catálogo → MPs → subrecetas → platos).

Uso:
  python recalcular_todos_costos.py --dry-run
  python recalcular_todos_costos.py --produccion
  python recalcular_todos_costos.py --produccion --sin-corregir-prov
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _run(cmd: list[str], label: str) -> None:
    print("\n" + "=" * 60)
    print(label)
    print("=" * 60)
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    time.sleep(2.0)


def main() -> None:
    p = argparse.ArgumentParser(description="Recalcular todos los costos teóricos Tatami")
    p.add_argument("--produccion", action="store_true")
    p.add_argument(
        "--corregir-prov",
        action="store_true",
        help="Opcional: corregir_precio_ref (solo pack evidente). Por defecto NO.",
    )
    args = p.parse_args()
    py = sys.executable
    prod = ["--produccion"] if args.produccion else []

    print(
        "\nPaso 0 recomendado si precio_ref está corrupto:\n"
        "  python procesar_facturas_drive.py --solo-precios-desde-xml --produccion\n"
    )
    if args.corregir_prov:
        _run(
            [py, "corregir_precio_ref_items_prov.py"] + prod,
            "1/5 — Corregir precio_ref (solo pack evidente)",
        )
    _run(
        [py, "sync_costos_mp_desde_items_prov.py"] + prod,
        "Sync BD_MP_SISTEMA desde items prov",
    )
    _run(
        [py, "calcular_costo_subrecetas.py"] + prod,
        "Costos subrecetas (BD_SUBRECETAS)",
    )
    _run(
        [py, "calcular_costo_recetas.py"] + prod,
        "Costos platos (BD_RECETAS)",
    )
    print("\nDiagnóstico opcional:")
    print("  python exportar_precio_ref_corruptos.py")
    print("  python diagnostico_costos_pipeline.py")
    print("\nCadena de costos completada.")


if __name__ == "__main__":
    main()

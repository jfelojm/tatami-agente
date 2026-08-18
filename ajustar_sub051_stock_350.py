"""
Ajusta SUB-051 @ BOD-002 a 350 ml.

Tras revertir descargos pre-producción (stock 810), se reconocen consumos
operativos reales (negronis servidos aunque el batch se registró tarde):
  - 270 ml venta 3-jun 21:01 (mismo día, antes del registro)
  - 190 ml consumos 29may–2jun (parcial)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
COD = "SUB-051"
BOD = "BOD-002"
STOCK_OBJETIVO = 350.0
DOC = "AJUSTE-SUB051-CONSUMOS-OPERATIVOS-20260707"

# Venta 3-jun 21:01 — consumo real mismo día de primera producción
VENTA_JUN3 = {
    "cod_venta": "VTA-20260603-7318-39320",
    "fecha": "2026-06-03",
    "hora": "21:01:00",
    "cantidad_mov": 270.0,
}
# Resto de consumos operativos pre-registro (29may–2jun)
AJUSTE_PARCIAL_ML = 190.0


def main() -> int:
    from recalcular_stock_sheets import build_stock_calculado
    from supabase import create_client

    p = argparse.ArgumentParser()
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    dry = not args.produccion

    sb = create_client(
        __import__("os").getenv("SUPABASE_URL"),
        __import__("os").getenv("SUPABASE_KEY"),
    )

    stock = float(build_stock_calculado().get((COD, BOD), 0.0))
    delta = stock - STOCK_OBJETIVO
    print(f"Stock actual {COD} @ {BOD}: {stock:.0f} ml")
    print(f"Objetivo: {STOCK_OBJETIVO:.0f} ml → registrar {delta:.0f} ml consumo adicional")

    if abs(delta) < 0.5:
        print("Ya está en objetivo.")
        return 0

    movs: list[dict] = []

    # 1) Reinsertar venta 3-jun
    ts = uuid.uuid4().hex[:12]
    movs.append(
        {
            "cod_mov": f"MOV-VTA-20260603-{COD}-{ts}",
            "fecha": f"{VENTA_JUN3['fecha']}T{VENTA_JUN3['hora']}",
            "tipo_mov": "SALIDA_VENTA",
            "cod_mp_sistema": COD,
            "nombre_mp": "Batch negroni",
            "cod_bodega_origen": BOD,
            "cod_bodega_destino": None,
            "cantidad_mov": VENTA_JUN3["cantidad_mov"],
            "unidad_base": "ml",
            "costo_unitario": 0.023965,
            "costo_total": round(VENTA_JUN3["cantidad_mov"] * 0.023965, 4),
            "origen_documento": "VENTA_SMART_MENU",
            "num_documento": VENTA_JUN3["cod_venta"],
            "registrado_por": "AGENTE",
            "observaciones": (
                "Consumo operativo 3-jun (batch registrado 23:48, cócteles sí servidos) | "
                "Classic Negroni rec 046"
            ),
        }
    )

    # 2) Ajuste parcial 29may–2jun
    ts2 = uuid.uuid4().hex[:12]
    movs.append(
        {
            "cod_mov": f"MOV-AJ-{COD}-{ts2}",
            "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "tipo_mov": "AJUSTE_NEGATIVO",
            "cod_mp_sistema": COD,
            "nombre_mp": "Batch negroni",
            "cod_bodega_origen": BOD,
            "cod_bodega_destino": None,
            "cantidad_mov": AJUSTE_PARCIAL_ML,
            "unidad_base": "ml",
            "costo_unitario": 0.023965,
            "costo_total": round(AJUSTE_PARCIAL_ML * 0.023965, 4),
            "origen_documento": "AJUSTE_MANUAL",
            "num_documento": DOC,
            "registrado_por": "AGENTE",
            "observaciones": (
                f"Consumos operativos pre-registro batch 29may–2jun ({AJUSTE_PARCIAL_ML:.0f} ml) | "
                f"stock objetivo {STOCK_OBJETIVO:.0f} ml"
            ),
        }
    )

    total_consumo = VENTA_JUN3["cantidad_mov"] + AJUSTE_PARCIAL_ML
    print(f"\nMovimientos a insertar ({total_consumo:.0f} ml):")
    for m in movs:
        print(f"  {m['tipo_mov']} {m['cantidad_mov']} ml | {m.get('num_documento')}")

    if dry:
        print(f"\n[DRY RUN] Stock esperado: {stock - total_consumo:.0f} ml")
        return 0

    for m in movs:
        sb.table("mov_inventario").insert(m).execute()

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "recalcular_stock_sheets.py"),
            "--produccion",
            "--cod-mp",
            COD,
        ],
        cwd=str(ROOT),
        check=True,
    )

    stock2 = float(build_stock_calculado().get((COD, BOD), 0.0))
    print(f"\nStock final: {stock2:.0f} ml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

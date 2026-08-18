"""Traslado último ingreso huevos (450 uni factura 103) BOD-001 -> BOD-005."""
from __future__ import annotations

import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

MP = "069"
CANT = 450.0
NUM = "103"
ORIGEN = "BOD-001"
DESTINO = "BOD-005"


def main() -> int:
    from supabase import create_client
    from inventario_traslado import registrar_traslado_mp
    from recalcular_stock_sheets import build_stock_calculado

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    stock = build_stock_calculado()
    print(f"Antes: {ORIGEN}={stock.get((MP, ORIGEN), 0)} {DESTINO}={stock.get((MP, DESTINO), 0)}")

    registrar_traslado_mp(
        sb,
        cod_mp=MP,
        bodega_origen=ORIGEN,
        bodega_destino=DESTINO,
        cantidad=CANT,
        nombre_mp="HUEVOS",
        unidad_base="uni",
        costo_unitario_ref=0.116667,
        registrado_por="AGENTE",
        recalcular_sheets=False,
    )
    print(f"Traslado OK {CANT} uni | ultimo ingreso factura {NUM}")

    subprocess.run(
        [sys.executable, "recalcular_stock_sheets.py", "--produccion"],
        cwd=os.path.dirname(__file__) or ".",
        check=True,
    )
    stock = build_stock_calculado()
    print(f"Despues: {ORIGEN}={stock.get((MP, ORIGEN), 0)} {DESTINO}={stock.get((MP, DESTINO), 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

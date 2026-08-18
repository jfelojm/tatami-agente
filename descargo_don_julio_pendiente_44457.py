"""
Descargo faltante: VTA-20260707-8376-44457 (1 SHOT Don Julio Reposado = 60 ml).
Marcada descargada por clave de línea duplicada con 44456 (2 SHOT), sin mov_inventario.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(override=True)

COD_VENTA = "VTA-20260707-8376-44457"
COD_MP = "165"
BOD = "BOD-002"
CONSUMO_ML = 60.0
COSTO_U = 0.1309


def main() -> int:
    from supabase import create_client
    from recalcular_stock_sheets import _clave_stock, build_stock_calculado

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    exist = (
        sb.table("mov_inventario")
        .select("cod_mov")
        .eq("num_documento", COD_VENTA)
        .eq("tipo_mov", "SALIDA_VENTA")
        .execute()
        .data
        or []
    )
    if exist:
        print(f"Ya existe mov para {COD_VENTA}: {exist[0]['cod_mov']}")
        return 0

    venta = sb.table("hist_ventas").select("*").eq("cod_venta", COD_VENTA).execute().data
    if not venta:
        print(f"No se encontró {COD_VENTA}")
        return 1
    v = venta[0]
    fecha = (v.get("fecha") or "2026-07-07").strip()[:10]
    hora = (v.get("hora") or "22:00").strip()
    parts = hora.split(":")
    if len(parts) == 2:
        iso = f"{fecha}T{int(parts[0]):02d}:{int(parts[1]):02d}:00"
    else:
        iso = f"{fecha}T{hora}"

    stock_antes = float(build_stock_calculado().get(_clave_stock(COD_MP, BOD), 0))
    print(f"Stock MP {COD_MP} @ {BOD} antes: {stock_antes:.0f} ml")

    mov = {
        "cod_mov": f"MOV-{fecha.replace('-', '')}-{COD_MP}-{uuid.uuid4().hex[:16]}",
        "fecha": iso,
        "tipo_mov": "SALIDA_VENTA",
        "cod_mp_sistema": COD_MP,
        "nombre_mp": "Tequila Don Julio Reposado",
        "cod_bodega_origen": BOD,
        "cod_bodega_destino": None,
        "cantidad_mov": CONSUMO_ML,
        "unidad_base": "ml",
        "costo_unitario": COSTO_U,
        "costo_total": round(CONSUMO_ML * COSTO_U, 4),
        "origen_documento": "VENTA_SMART_MENU",
        "num_documento": COD_VENTA,
        "registrado_por": "AGENTE",
        "observaciones": "Descargo automático receta 073 var=SHOT bod=BOD-002 (pendiente dedup ticket)",
    }
    sb.table("mov_inventario").insert(mov).execute()
    print(f"Insertado: {mov['cod_mov']} | −{CONSUMO_ML:.0f} ml")

    if not v.get("descargado"):
        sb.table("hist_ventas").update(
            {"descargado": True, "fecha_descargo": datetime.now(timezone.utc).isoformat()}
        ).eq("cod_venta", COD_VENTA).execute()

    root = os.path.dirname(__file__) or "."
    subprocess.run(
        [sys.executable, "recalcular_stock_sheets.py", "--produccion", "--cod-mp", COD_MP],
        cwd=root,
        check=True,
    )

    stock_desp = float(build_stock_calculado().get(_clave_stock(COD_MP, BOD), 0))
    print(f"Stock MP {COD_MP} @ {BOD} después: {stock_desp:.0f} ml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

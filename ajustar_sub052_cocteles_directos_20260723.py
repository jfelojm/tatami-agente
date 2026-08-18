"""
Cuadra 7 Tokio Mule (23–26 jul) hechos directo sin batch SUB-052.

Acciones:
  1. Elimina SALIDA_VENTA SUB-052 (630 ml) de las 7 ventas.
  2. Inserta descargo de MPs del batch (196 Sake, 197 Vodka, 046 5 especias)
     proporcional a 90 ml/cóctel (regla de tres vs rendimiento 1970 ml).

Uso:
  python ajustar_sub052_cocteles_directos_20260723.py --dry-run
  python ajustar_sub052_cocteles_directos_20260723.py --produccion
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = __import__("pathlib").Path(__file__).resolve().parent
COD_SUB = "SUB-052"
BOD_BATCH = "BOD-002"
RENDIMIENTO_ML = 1970.0
PORCION_BATCH_ML = 90.0
DOC_AJUSTE = "AJUSTE-SUB052-DIRECTO-20260729"

VTAS = (
    "VTA-20260723-8953-47350",
    "VTA-20260724-8956-47354",
    "VTA-20260724-8988-47530",
    "VTA-20260724-8989-47545",
    "VTA-20260724-8998-47582",
    "VTA-20260724-9000-47584",
    "VTA-20260726-9061-47909",
)

# MPs del detalle SUB-052 @ rendimiento 1970 ml
BATCH_MPS = (
    ("196", "Sake Choya", 750.0, "ml"),
    ("197", "Vodka Smirnoff 700ml", 1400.0, "ml"),
    ("046", "5 ESPECIES CHINAS", 100.0, "gr"),
)


def _sheet_float(v) -> float:
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, 0.0)


def _consumo_mp_batch(cantidad_lote: float) -> float:
    return cantidad_lote * PORCION_BATCH_ML / RENDIMIENTO_ML


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2
    dry = args.dry_run or not args.produccion

    from recalcular_stock_sheets import build_stock_calculado
    from whatsapp_webhook import conectar_supabase, leer_bd_mp_sistema

    sb = conectar_supabase()
    mp_rows = leer_bd_mp_sistema()
    mp_cost: dict[tuple[str, str], dict] = {}
    for r in mp_rows:
        cod = str(r.get("cod_mp_sistema") or "").strip()
        bod = str(r.get("cod_bodega") or "").strip().upper()
        if cod and bod:
            mp_cost[(cod, bod)] = r

    print("=" * 70)
    print(f"AJUSTE SUB-052 — cócteles directos (7 ventas) — {'DRY RUN' if dry else 'PRODUCCION'}")
    print("=" * 70)

    sub_movs: list[dict] = []
    for doc in VTAS:
        rows = (
            sb.table("mov_inventario")
            .select(
                "cod_mov,fecha,tipo_mov,cod_mp_sistema,cantidad_mov,"
                "num_documento,observaciones,cod_bodega_origen"
            )
            .eq("num_documento", doc)
            .eq("cod_mp_sistema", COD_SUB)
            .eq("tipo_mov", "SALIDA_VENTA")
            .execute()
            .data
            or []
        )
        sub_movs.extend(rows)

    print(f"\nSALIDA_VENTA {COD_SUB} a revertir: {len(sub_movs)} movs")
    ml_rev = 0.0
    for m in sorted(sub_movs, key=lambda x: x.get("fecha") or ""):
        c = abs(float(m.get("cantidad_mov") or 0))
        ml_rev += c
        print(f"  {(m.get('fecha') or '')[:16]} | {c:.0f} ml | {m.get('num_documento')} | {m.get('cod_mov')}")

    if len(sub_movs) != 7:
        print(f"ERROR: se esperaban 7 movs SUB-052, hay {len(sub_movs)}")
        return 1

    nuevos: list[dict] = []
    totales: dict[str, float] = {}
    for doc in VTAS:
        ref = next(m for m in sub_movs if m.get("num_documento") == doc)
        fecha = (ref.get("fecha") or "")[:19]
        if len(fecha) == 10:
            fecha = f"{fecha}T12:00:00"
        for cod_mp, nombre, cant_lote, unidad in BATCH_MPS:
            consumo = round(_consumo_mp_batch(cant_lote), 4)
            mp_info = mp_cost.get((cod_mp, BOD_BATCH), {})
            costo_u = _sheet_float(mp_info.get("costo_unitario_ref", 0))
            cod_mov = f"MOV-{fecha[:10].replace('-', '')}-{cod_mp}-{uuid.uuid4().hex[:12]}"
            mov = {
                "cod_mov": cod_mov,
                "fecha": fecha,
                "tipo_mov": "SALIDA_VENTA",
                "cod_mp_sistema": cod_mp,
                "nombre_mp": nombre,
                "cod_bodega_origen": BOD_BATCH,
                "cod_bodega_destino": None,
                "cantidad_mov": consumo,
                "unidad_base": unidad,
                "costo_unitario": costo_u,
                "costo_total": round(consumo * costo_u, 4),
                "origen_documento": "AJUSTE_MANUAL",
                "num_documento": doc,
                "registrado_por": "AGENTE",
                "observaciones": (
                    f"Descargo MP batch tokio mule directo (sin {COD_SUB}) | "
                    f"plato 048 | var=TOKIO MULE | ref {DOC_AJUSTE}"
                ),
            }
            nuevos.append(mov)
            totales[cod_mp] = totales.get(cod_mp, 0.0) + consumo

    print(f"\nDescargos MP batch a insertar: {len(nuevos)} movs")
    for cod_mp, nombre, cant_lote, unidad in BATCH_MPS:
        u = unidad
        print(f"  MP {cod_mp} {nombre}: {totales[cod_mp]:.2f} {u} total ({len(VTAS)} cócteles)")

    stock0 = {k: float(v) for k, v in build_stock_calculado().items() if k[0] in {COD_SUB, "196", "197", "046"}}
    print("\nStock actual (afectados):")
    for key in sorted(stock0):
        print(f"  {key[0]}@{key[1]}: {stock0[key]:.2f}")

    print("\nEfecto neto esperado:")
    print(f"  {COD_SUB}@{BOD_BATCH}: +{ml_rev:.0f} ml")
    for cod_mp, _, _, unidad in BATCH_MPS:
        print(f"  {cod_mp}@{BOD_BATCH}: -{totales[cod_mp]:.2f} {unidad}")

    if dry:
        print("\n[DRY-RUN] sin cambios")
        return 0

    cod_movs_del = [m["cod_mov"] for m in sub_movs if m.get("cod_mov")]
    for i in range(0, len(cod_movs_del), 40):
        sb.table("mov_inventario").delete().in_("cod_mov", cod_movs_del[i : i + 40]).execute()
    print(f"\nEliminados {len(cod_movs_del)} movs {COD_SUB}")

    for i in range(0, len(nuevos), 50):
        sb.table("mov_inventario").insert(nuevos[i : i + 50]).execute()
    print(f"Insertados {len(nuevos)} descargos MP batch directo")

    mps = sorted({COD_SUB, "196", "197", "046"})
    for cod in mps:
        subprocess.run(
            [sys.executable, str(ROOT / "recalcular_stock_sheets.py"), "--produccion", "--cod-mp", cod],
            cwd=str(ROOT),
            check=False,
        )

    stock1 = build_stock_calculado()
    print("\nStock final:")
    for cod in mps:
        s = float(stock1.get((cod, BOD_BATCH), 0.0))
        u = "ml" if cod != "046" else "gr"
        print(f"  {cod}@{BOD_BATCH}: {s:.2f} {u}")

    print(f"\nListo. {DOC_AJUSTE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

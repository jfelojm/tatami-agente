"""
Backfill ENTRADAs MP 065 faltantes por línea XML (incluye facturas ya parciales).

Uso:
  python backfill_panceta_065_entradas.py --dry-run
  python backfill_panceta_065_entradas.py --produccion
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

RUC = "0101298701001"
COD_MP = "065"
COD_ITEM = "01007025"
BOD = "BOD-001"
DOC_COMPENSA = "AJUSTE-COMPENSA-BACKFILL-PANCETA-065-LINEAS-20260805"
FACTOR = 1000.0


def _sb():
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _es_panceta(it: dict) -> bool:
    desc = (it.get("descripcion_proveedor") or "").upper()
    cod = (it.get("cod_item_xml") or "").strip()
    return "PANCET" in desc or "K5010" in desc or "01007025" in cod or cod.endswith(
        "1007025"
    )


def _entradas_qty_por_factura(sb) -> dict[str, Counter]:
    """num_factura -> Counter de cantidades ENTRADA (gr redondeado 1 decimal)."""
    out: dict[str, Counter] = {}
    offset = 0
    while True:
        batch = (
            sb.table("mov_inventario")
            .select("num_documento,cantidad_mov")
            .eq("cod_mp_sistema", COD_MP)
            .eq("tipo_mov", "ENTRADA")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        if not batch:
            break
        for m in batch:
            num = (m.get("num_documento") or "").strip()
            if not num:
                continue
            qty = round(float(m.get("cantidad_mov") or 0), 1)
            out.setdefault(num, Counter())[qty] += 1
        offset += len(batch)
        if len(batch) < 1000:
            break
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--sin-compensa", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from procesar_facturas_drive import parsear_xml_sri

    sb = _sb()
    ya = _entradas_qty_por_factura(sb)

    sri = []
    offset = 0
    while True:
        batch = (
            sb.table("sri_comprobantes_recibidos")
            .select("num_factura,fecha_emision,xml_autorizado")
            .eq("ruc_emisor", RUC)
            .range(offset, offset + 100)
            .execute()
            .data
            or []
        )
        if not batch:
            break
        sri.extend(batch)
        offset += len(batch)
        if len(batch) < 100:
            break

    plan: list[dict] = []
    for s in sorted(sri, key=lambda x: str(x.get("fecha_emision") or "")):
        num = (s.get("num_factura") or "").strip()
        xml = s.get("xml_autorizado") or ""
        if not num or not xml:
            continue
        fac = parsear_xml_sri(xml)
        fecha = str(fac.get("fecha_factura") or s.get("fecha_emision") or "")[:10]
        disponibles = Counter(ya.get(num, Counter()))
        for idx, it in enumerate(fac.get("items") or []):
            if not _es_panceta(it):
                continue
            kg = float(it.get("cantidad") or 0)
            if kg <= 0:
                continue
            gr = round(kg * FACTOR, 1)
            if disponibles[gr] > 0:
                disponibles[gr] -= 1
                continue  # ya cubierta por ENTRADA existente
            costo_ef = float(it.get("costo_efectivo") or 0)
            costo_u = round(costo_ef / FACTOR, 6) if FACTOR else 0
            total = float(it.get("precio_total_sin_impuesto") or 0)
            plan.append(
                {
                    "fecha": fecha,
                    "num": num,
                    "linea": idx,
                    "kg": kg,
                    "gr": round(kg * FACTOR, 4),
                    "costo_u": costo_u,
                    "costo_total": round(total, 4),
                    "desc": (it.get("descripcion_proveedor") or "").strip(),
                    "cod_xml": (it.get("cod_item_xml") or "").strip() or COD_ITEM,
                }
            )

    print("=" * 60)
    print(f"BACKFILL PANCETA 065 LINEAS — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print(f"Bodega: {BOD} | líneas faltantes: {len(plan)}")
    print("=" * 60)
    if not plan:
        print("Nada pendiente.")
        return 0

    tot = 0.0
    for row in plan:
        tot += row["gr"]
        print(
            f"  {row['fecha']} {row['num']} L{row['linea']} "
            f"{row['kg']:.4f} kg = {row['gr']:.1f} gr ${row['costo_total']}"
        )
    print(f"\nTotal: {tot:.1f} gr ({tot/1000:.2f} kg)")
    if not args.sin_compensa:
        print(f"Compensa AJUSTE_NEGATIVO {tot:.1f} gr @ {BOD}")

    if args.dry_run:
        return 0

    ya_comp = (
        sb.table("mov_inventario")
        .select("cod_mov")
        .eq("num_documento", DOC_COMPENSA)
        .eq("cod_mp_sistema", COD_MP)
        .limit(1)
        .execute()
        .data
        or []
    )

    insertados = 0
    for row in plan:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]
        obs = (
            f"{row['desc']} | ORIGEN:BACKFILL_PANCETA_065 | ITEM_XML:{row['cod_xml']} "
            f"| LINEA:{row['linea']} | qty_xml_kg={row['kg']}"
        )
        mov = {
            "cod_mov": f"MOV-{row['fecha'].replace('-', '')}-{COD_MP}-BF{row['linea']}-{ts}",
            "fecha": f"{row['fecha']}T00:00:00",
            "tipo_mov": "ENTRADA",
            "cod_mp_sistema": COD_MP,
            "nombre_mp": "PANCETA DE CERDO",
            "cod_bodega_origen": None,
            "cod_bodega_destino": BOD,
            "cantidad_mov": row["gr"],
            "unidad_base": "gr",
            "costo_unitario": row["costo_u"],
            "costo_total": row["costo_total"],
            "origen_documento": "FACTURA",
            "num_documento": row["num"],
            "registrado_por": "AGENTE_BACKFILL",
            "observaciones": obs,
        }
        sb.table("mov_inventario").insert(mov).execute()
        insertados += 1
        print(f"  OK ENTRADA {row['num']} L{row['linea']} +{row['gr']:.1f} gr")

    if not args.sin_compensa and insertados and not ya_comp:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]
        gr_comp = round(sum(r["gr"] for r in plan), 4)
        aj = {
            "cod_mov": f"MOV-AJ-065-COMP2-{ts}",
            "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "tipo_mov": "AJUSTE_NEGATIVO",
            "cod_mp_sistema": COD_MP,
            "nombre_mp": "PANCETA DE CERDO",
            "cod_bodega_origen": BOD,
            "cod_bodega_destino": None,
            "cantidad_mov": gr_comp,
            "unidad_base": "gr",
            "costo_unitario": 0.00783,
            "costo_total": round(gr_comp * 0.00783, 4),
            "origen_documento": "AJUSTE_MANUAL",
            "num_documento": DOC_COMPENSA,
            "registrado_por": "AGENTE_BACKFILL",
            "observaciones": (
                "Compensa backfill líneas panceta mar (dedupe ITEM_XML): "
                "ya reflejado en conteos físicos"
            ),
        }
        sb.table("mov_inventario").insert(aj).execute()
        print(f"  OK AJUSTE_NEGATIVO compensa -{gr_comp:.1f} gr")

    print(f"\nInsertadas {insertados}. Recalculando Sheets...")
    import subprocess

    subprocess.run(
        [sys.executable, "recalcular_stock_sheets.py", "--produccion", "--cod-mp", COD_MP],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

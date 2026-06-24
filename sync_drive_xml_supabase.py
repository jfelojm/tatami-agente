"""
Sube XML de facturas desde Google Drive a sri_comprobantes_recibidos (sin mover inventario).

Sirve para tener el respaldo en Supabase antes de backfill ENTRADA_COSTO_HIST.

Uso:
  python sync_drive_xml_supabase.py --dry-run
  python sync_drive_xml_supabase.py --produccion
  python sync_drive_xml_supabase.py --produccion --desde 2026-01-01 --hasta 2026-02-28
"""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv(override=True)


def _parse_fecha(s: str) -> date | None:
    s = (s or "").strip()[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--desde", default="")
    p.add_argument("--hasta", default="")
    args = p.parse_args()

    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    desde = _parse_fecha(args.desde) if args.desde else None
    hasta = _parse_fecha(args.hasta) if args.hasta else None

    from procesar_facturas_drive import descargar_xml, listar_xmls_pendientes, parsear_xml_sri
    from procesar_facturas_sri import guardar_descarga
    from sri_client import ComprobanteRecibido, SriConfig
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    config = SriConfig.from_env()
    meta = {"origen": "DRIVE_SYNC", "fase": "sync_xml"}

    nuevos = omitidos = fuera = errores = 0
    archivos = listar_xmls_pendientes()
    print(f"XML en Drive: {len(archivos)}")

    for i, arch in enumerate(archivos, 1):
        try:
            xml = descargar_xml(arch["id"])
            factura = parsear_xml_sri(xml)
            if not factura:
                errores += 1
                continue
            fecha_f = _parse_fecha(factura.get("fecha_factura", ""))
            if desde and fecha_f and fecha_f < desde:
                fuera += 1
                continue
            if hasta and fecha_f and fecha_f > hasta:
                fuera += 1
                continue
            clave = (factura.get("clave_acceso") or factura.get("num_autorizacion") or "").strip()
            if not clave or len(clave) < 40:
                import re

                found = re.findall(r"\b(\d{49})\b", xml)
                clave = found[0] if found else ""
            if not clave or len(clave) < 40:
                errores += 1
                print(f"  [{i}] SKIP sin clave: {arch.get('name')}")
                continue
            comp = ComprobanteRecibido(
                clave_acceso=clave,
                num_factura=(factura.get("num_factura") or "").strip(),
                ruc_emisor=(factura.get("ruc") or "").strip(),
                razon_social=(factura.get("razon_social") or "").strip(),
                fecha_emision=(factura.get("fecha_factura") or "")[:10],
            )
            prev = (
                sb.table("sri_comprobantes_recibidos")
                .select("clave_acceso,xml_autorizado")
                .eq("clave_acceso", clave)
                .limit(1)
                .execute()
                .data
                or []
            )
            if prev and prev[0].get("xml_autorizado"):
                omitidos += 1
                continue
            guardar_descarga(sb, comp, xml, meta, dry_run=args.dry_run)
            nuevos += 1
            if nuevos <= 5 or (fecha_f and fecha_f.month <= 2):
                print(
                    f"  [{'DRY' if args.dry_run else 'OK'}] {comp.num_factura} | "
                    f"{comp.fecha_emision} | {comp.razon_social[:35]}"
                )
        except Exception as e:
            errores += 1
            print(f"  ERROR {arch.get('name')}: {e}")

    print(
        f"\nResumen: {nuevos} nuevos, {omitidos} ya en DB, "
        f"{fuera} fuera de rango, {errores} errores"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

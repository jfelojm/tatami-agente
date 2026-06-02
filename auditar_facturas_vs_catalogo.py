"""
Compara XMLs en Drive (facturas) con BD_ITEMS_PROV y reglas de inventario.

Genera un CSV para ver por qué no hay precio_ref / costo_unitario_ref:
- línea sin match en catálogo
- cod_mp_sistema vacío en BD_ITEMS_PROV
- factor_conversion / unidad_compra vacíos (bloqueaba mov y, antes, también precio_ref)

Uso (desde tatami-agente):
  python auditar_facturas_vs_catalogo.py
  python auditar_facturas_vs_catalogo.py -o auditoria_facturas.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> None:
    from procesar_facturas_drive import (
        buscar_item_prov,
        cargar_bd_items_prov,
        cargar_lookup_ruc,
        conversion_compra_definida,
        descargar_xml,
        listar_xmls_pendientes,
        parsear_xml_sri,
    )

    p = argparse.ArgumentParser(description="Auditoría facturas Drive vs BD_ITEMS_PROV")
    p.add_argument(
        "-o",
        "--output",
        default="",
        help="Ruta CSV (default: auditar_facturas_vs_catalogo_<timestamp>.csv)",
    )
    args = p.parse_args()

    if not os.getenv("GOOGLE_DRIVE_FACTURAS_FOLDER_ID"):
        raise SystemExit("Falta GOOGLE_DRIVE_FACTURAS_FOLDER_ID en .env")

    cargar_bd_items_prov()
    cargar_lookup_ruc()

    xmls = listar_xmls_pendientes()
    if not xmls:
        print("No hay XMLs en la carpeta de facturas.")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = (args.output or "").strip() or f"auditar_facturas_vs_catalogo_{ts}.csv"

    fieldnames = [
        "archivo_drive",
        "num_factura",
        "fecha_factura",
        "ruc",
        "cod_item_xml",
        "descripcion_xml",
        "cantidad_xml",
        "precio_unitario_xml",
        "costo_efectivo_xml",
        "match_catalogo",
        "cod_item_prov",
        "cod_mp_sistema",
        "precio_ref_hoja",
        "factor_conversion_hoja",
        "unidad_compra_hoja",
        "conversion_ok_para_inventario",
        "motivo_sin_inventario",
    ]

    rows_out = 0
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        for archivo in xmls:
            nombre = archivo.get("name", "")
            try:
                texto = descargar_xml(archivo["id"])
            except Exception as e:
                print(f"WARN: no se pudo leer {nombre}: {e}")
                continue
            factura = parsear_xml_sri(texto)
            if not factura:
                continue

            for item in factura["items"]:
                item_prov = buscar_item_prov(
                    factura["ruc"],
                    item["cod_item_xml"],
                    item["descripcion_proveedor"],
                    factura.get("razon_social", ""),
                    factura.get("num_factura", ""),
                )

                if not item_prov:
                    w.writerow(
                        {
                            "archivo_drive": nombre,
                            "num_factura": factura["num_factura"],
                            "fecha_factura": factura["fecha_factura"],
                            "ruc": factura["ruc"],
                            "cod_item_xml": item["cod_item_xml"],
                            "descripcion_xml": item["descripcion_proveedor"],
                            "cantidad_xml": item["cantidad"],
                            "precio_unitario_xml": item["precio_unitario_xml"],
                            "costo_efectivo_xml": item["costo_efectivo"],
                            "match_catalogo": "NO",
                            "cod_item_prov": "",
                            "cod_mp_sistema": "",
                            "precio_ref_hoja": "",
                            "factor_conversion_hoja": "",
                            "unidad_compra_hoja": "",
                            "conversion_ok_para_inventario": "",
                            "motivo_sin_inventario": "Sin fila en BD_ITEMS_PROV (pendiente de alta / match)",
                        }
                    )
                    rows_out += 1
                    continue

                ok_conv, motivo = conversion_compra_definida(item_prov)
                cod_mp = (item_prov.get("cod_mp_sistema") or "").strip()
                motivo_inv = ""
                if not cod_mp:
                    motivo_inv = "BD_ITEMS_PROV sin cod_mp_sistema"
                elif not ok_conv:
                    motivo_inv = motivo

                w.writerow(
                    {
                        "archivo_drive": nombre,
                        "num_factura": factura["num_factura"],
                        "fecha_factura": factura["fecha_factura"],
                        "ruc": factura["ruc"],
                        "cod_item_xml": item["cod_item_xml"],
                        "descripcion_xml": item["descripcion_proveedor"],
                        "cantidad_xml": item["cantidad"],
                        "precio_unitario_xml": item["precio_unitario_xml"],
                        "costo_efectivo_xml": item["costo_efectivo"],
                        "match_catalogo": "SI",
                        "cod_item_prov": (item_prov.get("cod_item_prov") or "").strip(),
                        "cod_mp_sistema": cod_mp,
                        "precio_ref_hoja": (item_prov.get("precio_ref") or "").strip(),
                        "factor_conversion_hoja": (item_prov.get("factor_conversion") or "").strip(),
                        "unidad_compra_hoja": (item_prov.get("unidad_compra") or "").strip(),
                        "conversion_ok_para_inventario": "SI" if ok_conv and cod_mp else "NO",
                        "motivo_sin_inventario": motivo_inv,
                    }
                )
                rows_out += 1

    print(f"Líneas escritas: {rows_out}")
    print(f"Archivo: {os.path.abspath(out)}")
    print(
        "Filtra en Excel: match_catalogo=NO → alta en BD_ITEMS_PROV; "
        "conversion_ok=NO → completa factor y unidad_compra; "
        "cod_mp_sistema vacío → enlaza a BD_MP_SISTEMA."
    )


if __name__ == "__main__":
    main()

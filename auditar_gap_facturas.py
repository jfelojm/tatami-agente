"""
Audita huecos entre facturas procesadas, BD_ITEMS_PENDIENTES y mov_inventario.

Detecta:
- Líneas con match en catálogo hoy pero sin ENTRADA en mov_inventario
- Líneas sin match hoy que no están en BD_ITEMS_PENDIENTES
- Pendientes REGISTRADO sin entrada (catálogo vinculado pero factura no reprocesada)

Uso:
  python auditar_gap_facturas.py
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> None:
    from supabase import create_client

    from procesar_facturas_drive import (
        BD_ITEMS_PENDIENTES_SHEET,
        _get_sheet,
        _pendientes_header_row_idx,
        buscar_item_prov,
        cargar_bd_items_prov,
        cargar_lookup_ruc,
        conversion_compra_definida,
        descargar_xml,
        listar_xmls_pendientes,
        mov_entrada_factura_linea_ya_registrada,
        parsear_xml_sri,
    )

    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    cargar_bd_items_prov()
    cargar_lookup_ruc()

    sh = _get_sheet()
    ws = sh.worksheet(BD_ITEMS_PENDIENTES_SHEET)
    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        raise SystemExit("Sin cabecera en BD_ITEMS_PENDIENTES")
    headers = [(c or "").strip() for c in vals[hi]]
    idx = {h: headers.index(h) for h in headers}

    def pendiente_row(num_f: str, cod_xml: str) -> dict | None:
        for row in vals[hi + 1 :]:
            if not row or not any((c or "").strip() for c in row):
                continue
            nf = (row[idx["num_factura"]] if idx["num_factura"] < len(row) else "").strip()
            cx = (row[idx["cod_item_xml"]] if idx["cod_item_xml"] < len(row) else "").strip()
            if nf == num_f and cx == cod_xml.strip():
                return {
                    "estado": (row[idx["estado"]] if idx["estado"] < len(row) else "").strip(),
                    "fecha_registro": (
                        row[idx["fecha_registro"]] if idx["fecha_registro"] < len(row) else ""
                    ).strip(),
                    "cod_mp_asignado": (
                        row[idx.get("cod_mp_asignado", idx["estado"])]
                        if "cod_mp_asignado" in idx
                        else ""
                    ),
                }
        return None

    gaps: list[dict] = []
    no_pendiente: list[dict] = []
    registrado_sin_entrada: list[dict] = []

    xmls = listar_xmls_pendientes()
    print(f"XMLs en Drive: {len(xmls)}")

    for archivo in xmls:
        try:
            texto = descargar_xml(archivo["id"])
        except Exception as e:
            print(f"WARN lectura {archivo.get('name')}: {e}")
            continue
        factura = parsear_xml_sri(texto)
        if not factura:
            continue

        num_f = factura["num_factura"]
        ruc_f = factura["ruc"]
        fp_res = (
            sb.table("facturas_procesadas")
            .select("estado,items_procesados,items_sin_match,fecha_proceso")
            .eq("num_factura", num_f)
            .eq("ruc_proveedor", ruc_f)
            .execute()
        )
        fp_row = fp_res.data[0] if fp_res.data else None
        if not fp_row:
            continue

        for item in factura.get("items", []):
            cod_xml = (item.get("cod_item_xml") or "").strip()
            ip = buscar_item_prov(
                ruc_f,
                cod_xml,
                item.get("descripcion_proveedor", ""),
                factura.get("razon_social", ""),
                num_f,
            )
            pend = pendiente_row(num_f, cod_xml)

            if not ip:
                if not pend:
                    no_pendiente.append(
                        {
                            "factura": num_f,
                            "fecha": factura.get("fecha_factura"),
                            "cod": cod_xml[-10:],
                            "desc": (item.get("descripcion_proveedor") or "")[:40],
                            "estado_fp": fp_row["estado"],
                        }
                    )
                continue

            cod_mp = (ip.get("cod_mp_sistema") or "").strip()
            if not cod_mp:
                continue
            ok_conv, motivo = conversion_compra_definida(ip)
            if not ok_conv:
                continue

            ya = mov_entrada_factura_linea_ya_registrada(num_f, cod_mp, item)
            if ya:
                continue

            row = {
                "factura": num_f,
                "fecha": factura.get("fecha_factura"),
                "proveedor": (factura.get("razon_social") or "")[:28],
                "cod_xml": cod_xml[-10:],
                "desc": (item.get("descripcion_proveedor") or "")[:32],
                "mp": cod_mp,
                "cant": item.get("cantidad"),
                "estado_fp": fp_row["estado"],
                "pendiente_estado": (pend or {}).get("estado", ""),
                "fecha_reg_pend": (pend or {}).get("fecha_registro", ""),
            }
            gaps.append(row)
            if pend and (pend.get("estado") or "").strip().upper() == "REGISTRADO":
                registrado_sin_entrada.append(row)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join("logs", f"audit_gap_facturas_{ts}.csv")
    os.makedirs("logs", exist_ok=True)
    if gaps:
        import csv

        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(gaps[0].keys()))
            w.writeheader()
            w.writerows(gaps)

    print(f"\n=== RESUMEN AUDITORÍA ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ===")
    print(f"Líneas con match catálogo HOY pero SIN entrada mov: {len(gaps)}")
    print(f"  → de ellas, pendientes REGISTRADO (catálogo vinculado, sin reproceso): {len(registrado_sin_entrada)}")
    print(f"Líneas sin match HOY y SIN fila en BD_ITEMS_PENDIENTES: {len(no_pendiente)}")

    by_fac = Counter(g["factura"] for g in gaps)
    if by_fac:
        print("\nFacturas con huecos (líneas sin entrada):")
        for fac, n in by_fac.most_common(20):
            print(f"  {fac}: {n} líneas")

    if registrado_sin_entrada:
        print("\nEjemplos REGISTRADO sin entrada (patrón Eljuri):")
        for r in registrado_sin_entrada[:12]:
            print(
                f"  {r['factura']} MP{r['mp']} +{r['cant']} | pend={r['pendiente_estado']} | {r['desc']}"
            )

    if no_pendiente:
        print("\nSin match y sin fila pendientes (hueco en hoja):")
        for r in no_pendiente[:10]:
            print(f"  {r['factura']} {r['cod']} {r['desc']}")

    if gaps:
        print(f"\nCSV: {os.path.abspath(out)}")


if __name__ == "__main__":
    main()

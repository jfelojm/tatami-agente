"""
Ingresa stock pendiente desde BD_ITEMS_PENDIENTES (estado REGISTRADO).

No requiere XML en Drive ni reproceso SRI: usa cantidad/costo guardados en la hoja
al registrar el ítem pendiente, más el catálogo actual en BD_ITEMS_PROV.

Salvaguardas:
  - Solo filas REGISTRADO con cod_mp_asignado y match en catálogo
  - mov_entrada_factura_linea_ya_registrada evita duplicar ENTRADA
  - conversion_compra_definida y fecha_factura_permite_ingreso_stock
  - recalcular_stock_sheets suma mov_inventario (sin doble conteo)

Uso:
  python ingresar_stock_desde_pendientes.py --dry-run
  python ingresar_stock_desde_pendientes.py
  python ingresar_stock_desde_pendientes.py --recalcular
  python ingresar_stock_desde_pendientes.py --desde 2026-06-01 --dry-run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


def _parse_fecha(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_numero(s: str | None) -> float:
    t = (s or "").strip().replace(" ", "")
    if not t:
        return 0.0
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def _cargar_filas_pendientes_registrados() -> tuple[list[str], list[dict]]:
    from procesar_facturas_drive import (
        BD_ITEMS_PENDIENTES_SHEET,
        _get_sheet,
        _pendientes_header_row_idx,
    )

    sh = _get_sheet()
    ws = sh.worksheet(BD_ITEMS_PENDIENTES_SHEET)
    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        raise RuntimeError("Sin cabecera en BD_ITEMS_PENDIENTES")
    headers = [(c or "").strip() for c in vals[hi]]
    rows: list[dict] = []
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        d = {headers[j]: (row[j] if j < len(row) else "").strip() for j in range(len(headers))}
        d["_sheet_row"] = i
        if (d.get("estado") or "").strip().upper() != "REGISTRADO":
            continue
        rows.append(d)
    return headers, rows


def _resolver_item_prov(
    pend: dict,
    items_prov: list[dict],
) -> dict | None:
    from procesar_facturas_drive import buscar_item_prov

    ruc = pend.get("ruc_proveedor", "")
    cod_xml = pend.get("cod_item_xml", "")
    desc = pend.get("descripcion_xml", "")
    num = pend.get("num_factura", "")
    razon = pend.get("razon_social", "")

    ip = buscar_item_prov(ruc, cod_xml, desc, razon, num)
    if ip:
        return ip

    cod_mp = (pend.get("cod_mp_asignado") or "").strip()
    if not cod_mp:
        return None
    for row in items_prov:
        if (row.get("cod_mp_sistema") or "").strip() == cod_mp and (
            (row.get("ruc_proveedor") or "").strip() == ruc
            or (row.get("cod_proveedor") or "").strip() == (pend.get("cod_proveedor") or "").strip()
        ):
            return row
    for row in items_prov:
        if (row.get("cod_mp_sistema") or "").strip() == cod_mp:
            return row
    return None


def _item_desde_pendiente(pend: dict) -> dict:
    cantidad = _parse_numero(pend.get("cantidad"))
    costo_ef = _parse_numero(pend.get("costo_efectivo"))
    precio_u = _parse_numero(pend.get("precio_unitario_xml"))
    total = _parse_numero(pend.get("costo_total_xml"))
    if total <= 0 and cantidad > 0 and costo_ef > 0:
        total = round(cantidad * costo_ef, 4)
    if costo_ef <= 0 and cantidad > 0 and total > 0:
        costo_ef = round(total / cantidad, 6)
    if precio_u <= 0:
        precio_u = costo_ef
    return {
        "cod_item_xml": pend.get("cod_item_xml", ""),
        "descripcion_proveedor": pend.get("descripcion_xml", ""),
        "cantidad": cantidad,
        "precio_unitario_xml": precio_u,
        "descuento": 0.0,
        "precio_total_sin_impuesto": total,
        "costo_efectivo": costo_ef,
    }


def _factura_desde_pendiente(pend: dict) -> dict:
    return {
        "num_factura": pend.get("num_factura", ""),
        "ruc": pend.get("ruc_proveedor", ""),
        "razon_social": pend.get("razon_social", ""),
        "fecha_factura": (pend.get("fecha_factura") or "")[:10],
    }


def analizar_elegibles(
    *,
    desde: date | None = None,
    hasta: date | None = None,
) -> dict:
    from procesar_facturas_drive import (
        cargar_bd_items_prov,
        cargar_lookup_ruc,
        conversion_compra_definida,
        fecha_factura_permite_ingreso_stock,
        mov_entrada_factura_linea_ya_registrada,
    )

    cargar_bd_items_prov()
    cargar_lookup_ruc()
    items_prov = cargar_bd_items_prov()
    _, filas = _cargar_filas_pendientes_registrados()

    elegibles: list[dict] = []
    omitidas: list[dict] = []

    for pend in filas:
        num = pend.get("num_factura", "")
        f_dt = _parse_fecha(pend.get("fecha_factura"))
        if desde and f_dt and f_dt < desde:
            omitidas.append({"num_factura": num, "motivo": "fuera_ventana_desde"})
            continue
        if hasta and f_dt and f_dt > hasta:
            omitidas.append({"num_factura": num, "motivo": "fuera_ventana_hasta"})
            continue

        item = _item_desde_pendiente(pend)
        if item["cantidad"] <= 0:
            omitidas.append({"num_factura": num, "motivo": "cantidad_invalida", "cod": pend.get("cod_item_xml")})
            continue

        ip = _resolver_item_prov(pend, items_prov)
        if not ip:
            omitidas.append({"num_factura": num, "motivo": "sin_catalogo", "cod": pend.get("cod_item_xml")})
            continue

        cod_mp = (ip.get("cod_mp_sistema") or "").strip()
        if not cod_mp:
            omitidas.append({"num_factura": num, "motivo": "sin_cod_mp", "cod": pend.get("cod_item_xml")})
            continue

        factura = _factura_desde_pendiente(pend)
        if mov_entrada_factura_linea_ya_registrada(num, cod_mp, item):
            omitidas.append({"num_factura": num, "motivo": "ya_ingresado", "cod_mp": cod_mp})
            continue

        if not fecha_factura_permite_ingreso_stock(factura.get("fecha_factura", "")):
            omitidas.append({"num_factura": num, "motivo": "fecha_bloqueada"})
            continue

        ok_conv, motivo = conversion_compra_definida(ip)
        if not ok_conv:
            omitidas.append({"num_factura": num, "motivo": "sin_conversion", "detalle": motivo})
            continue

        elegibles.append(
            {
                "pend": pend,
                "factura": factura,
                "item": item,
                "item_prov": ip,
                "cod_mp": cod_mp,
            }
        )

    return {
        "elegibles": elegibles,
        "omitidas": omitidas,
        "total_registrado": len(filas),
    }


def ejecutar_ingresos(elegibles: list[dict], *, dry_run: bool) -> dict:
    from bodegas_config import resolver_bodega_entrada_linea
    from procesar_facturas_drive import (
        _flush_mp_sistema,
        _mp_cache_key,
        _parse_factor_positivo,
        procesar_variacion_precio,
        registrar_entrada_inventario,
    )

    ok = 0
    err = 0
    deltas_stock: dict[tuple[str, str], float] = {}
    deltas_costo: dict[tuple[str, str], float] = {}

    for row in elegibles:
        pend = row["pend"]
        factura = row["factura"]
        item = row["item"]
        ip = row["item_prov"]
        cod_mp = row["cod_mp"]
        num = factura["num_factura"]
        desc = (item.get("descripcion_proveedor") or "")[:40]

        bodega_dest, err_bod = resolver_bodega_entrada_linea(ip, bodega_override=None, confirmada=False)
        if err_bod or not bodega_dest:
            print(f"  SKIP {num} MP{cod_mp} ({desc}): bodega — {err_bod}")
            err += 1
            continue

        factor = _parse_factor_positivo(ip.get("factor_conversion"))
        if factor is None:
            print(f"  SKIP {num} MP{cod_mp}: factor_conversion inválido")
            err += 1
            continue
        cantidad_base = item["cantidad"] * factor
        costo_u = item["costo_efectivo"] / factor if factor else 0

        if dry_run:
            print(
                f"  [DRY] {num} MP{cod_mp} +{round(cantidad_base, 4)} "
                f"({item['cantidad']} x factor {factor}) | {desc}"
            )
            ok += 1
            continue

        procesar_variacion_precio(ip, factura, item)
        if registrar_entrada_inventario(ip, item, factura, cod_bodega_destino=bodega_dest):
            key = _mp_cache_key(cod_mp, bodega_dest)
            deltas_stock[key] = deltas_stock.get(key, 0.0) + cantidad_base
            deltas_costo[key] = costo_u
            print(f"  OK {num} MP{cod_mp} +{round(cantidad_base, 4)} | {desc}")
            ok += 1
        else:
            err += 1

    if not dry_run and (deltas_stock or deltas_costo):
        _flush_mp_sistema(deltas_stock, deltas_costo)

    return {"ok": ok, "err": err}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--desde", metavar="YYYY-MM-DD")
    parser.add_argument("--hasta", metavar="YYYY-MM-DD")
    parser.add_argument("--recalcular", action="store_true")
    args = parser.parse_args(argv)

    desde = _parse_fecha(args.desde)
    hasta = _parse_fecha(args.hasta)

    print("=" * 60)
    print("INGRESO STOCK DESDE BD_ITEMS_PENDIENTES (REGISTRADO)")
    print("=" * 60)
    print("Fuente: hoja pendientes (cantidad/costo) + catálogo BD_ITEMS_PROV")
    print("No requiere XML en Drive; anti-duplicado por mov_inventario")
    if desde or hasta:
        print(f"Ventana fecha factura: {desde or '...'} .. {hasta or '...'}")
    print()

    analisis = analizar_elegibles(desde=desde, hasta=hasta)
    elegibles = analisis["elegibles"]
    omitidas = analisis["omitidas"]

    print(
        f"REGISTRADO en hoja: {analisis['total_registrado']} | "
        f"elegibles ingreso: {len(elegibles)} | omitidas: {len(omitidas)}"
    )

    motivos: dict[str, int] = {}
    for o in omitidas:
        m = o.get("motivo", "?")
        motivos[m] = motivos.get(m, 0) + 1
    if motivos:
        print("Omitidas por motivo:")
        for m, c in sorted(motivos.items(), key=lambda x: -x[1]):
            print(f"  {m}: {c}")

    if elegibles:
        print("\nLineas a ingresar:")
        for e in elegibles[:20]:
            p = e["pend"]
            it = e["item"]
            print(
                f"  {p.get('num_factura')} MP{e['cod_mp']} "
                f"+{it['cantidad']} | {p.get('descripcion_xml', '')[:35]}"
            )
        if len(elegibles) > 20:
            print(f"  ... y {len(elegibles) - 20} mas")

    if not elegibles:
        print("\nNada que ingresar.")
        return 0

    print()
    res = ejecutar_ingresos(elegibles, dry_run=args.dry_run)
    print(f"\nResultado: ok={res['ok']} err={res['err']}")

    if args.dry_run:
        print("[DRY RUN] No se escribio mov_inventario ni BD_MP_SISTEMA.")
        return 0

    if args.recalcular:
        root = Path(__file__).resolve().parent
        py = root / "venv" / "Scripts" / "python.exe"
        if not py.is_file():
            py = Path(sys.executable)
        print("\nRecalculando stock desde mov_inventario...")
        r = subprocess.run(
            [str(py), str(root / "recalcular_stock_sheets.py"), "--produccion"],
            cwd=str(root),
        )
        if r.returncode != 0:
            return r.returncode

    return 0 if res["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

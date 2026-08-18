"""
Reprocesa facturas PARCIAL tras promover ítems a BD_ITEMS_PROV.

Solo encola (DESCARGADO) facturas que tienen al menos una línea con:
  - match en catálogo HOY,
  - conversión compra definida,
  - sin ENTRADA previa en mov_inventario (anti-duplicado),
  - y pendiente en REGISTRADO (post-promoción), salvo --incluir-sin-fila-pendientes.

No toca facturas COMPLETA ni PARCIAL sin líneas nuevas que ingresar.

Uso:
  python reprocesar_facturas_parciales_sri.py --dry-run
  python reprocesar_facturas_parciales_sri.py
  python reprocesar_facturas_parciales_sri.py --desde 2026-06-01 --recalcular
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv(override=True)


def _supabase():
    from supabase import create_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Faltan SUPABASE_URL / SUPABASE_KEY en .env")
    return create_client(url, key)


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


def _cargar_mapa_pendientes() -> dict[str, str]:
    """clave_item_pendiente -> estado (PENDIENTE, REGISTRADO, IGNORADO)."""
    from procesar_facturas_drive import (
        BD_ITEMS_PENDIENTES_SHEET,
        _clave_item_pendiente,
        _get_sheet,
        _pendientes_header_row_idx,
    )

    sh = _get_sheet()
    ws = sh.worksheet(BD_ITEMS_PENDIENTES_SHEET)
    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        return {}
    headers = [(c or "").strip() for c in vals[hi]]
    try:
        idx_estado = headers.index("estado")
    except ValueError:
        idx_estado = -1

    out: dict[str, str] = {}
    for row in vals[hi + 1 :]:
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        d = {headers[j]: (row[j] if j < len(row) else "").strip() for j in range(len(headers))}
        num = d.get("num_factura", "")
        if not num:
            continue
        item = {
            "cod_item_xml": d.get("cod_item_xml", ""),
            "descripcion_proveedor": d.get("descripcion_xml", d.get("descripcion_proveedor", "")),
        }
        factura = {
            "num_factura": num,
            "ruc": d.get("ruc_proveedor", ""),
        }
        clave = _clave_item_pendiente(factura, item)
        if idx_estado >= 0 and idx_estado < len(row):
            out[clave] = (row[idx_estado] or "").strip().upper()
    return out


def _clasificar_linea(
    factura: dict,
    item: dict,
    *,
    pendientes_estado: dict[str, str],
    solo_promovidos: bool,
) -> str:
    """
    Retorna:
      ignorado | pendiente_sin_promover | sin_catalogo | sin_conversion |
      fecha_bloqueada | ya_ingresado | nueva_entrada
    """
    from procesar_facturas_drive import (
        _clave_item_pendiente,
        buscar_item_prov,
        conversion_compra_definida,
        fecha_factura_permite_ingreso_stock,
        mov_entrada_factura_linea_ya_registrada,
    )

    clave_p = _clave_item_pendiente(factura, item)
    est_p = pendientes_estado.get(clave_p, "")

    if est_p == "IGNORADO":
        return "ignorado"

    item_prov = buscar_item_prov(
        factura["ruc"],
        item["cod_item_xml"],
        item["descripcion_proveedor"],
        factura.get("razon_social", ""),
        factura.get("num_factura", ""),
    )
    if not item_prov:
        return "sin_catalogo"

    cod_mp = (item_prov.get("cod_mp_sistema") or "").strip()
    if not cod_mp:
        return "sin_catalogo"

    if solo_promovidos and est_p != "REGISTRADO":
        return "pendiente_sin_promover"

    if mov_entrada_factura_linea_ya_registrada(
        factura["num_factura"], cod_mp, item
    ):
        return "ya_ingresado"

    if not fecha_factura_permite_ingreso_stock(factura.get("fecha_factura", "")):
        return "fecha_bloqueada"

    ok_conv, _ = conversion_compra_definida(item_prov)
    if not ok_conv:
        return "sin_conversion"

    return "nueva_entrada"


def analizar_parciales_elegibles(
    *,
    desde: date | None = None,
    hasta: date | None = None,
    solo_promovidos: bool = True,
    limite: int = 500,
) -> dict:
    from procesar_facturas_drive import (
        cargar_bd_items_prov,
        cargar_lookup_ruc,
        parsear_xml_sri,
    )

    sb = _supabase()
    cargar_bd_items_prov()
    cargar_lookup_ruc()
    pendientes_estado = _cargar_mapa_pendientes()

    q = (
        sb.table("facturas_procesadas")
        .select("num_factura,ruc_proveedor,fecha_factura,estado,items_sin_match")
        .eq("estado", "PARCIAL")
        .order("fecha_factura", desc=True)
        .limit(limite)
    )
    parciales = q.execute().data or []

    elegibles: list[dict] = []
    omitidas: list[dict] = []
    conteo_lineas: dict[str, int] = {}

    for fp in parciales:
        num = (fp.get("num_factura") or "").strip()
        ruc = (fp.get("ruc_proveedor") or "").strip()
        f_raw = (fp.get("fecha_factura") or "")[:10]
        f_dt = _parse_fecha(f_raw)
        if desde and f_dt and f_dt < desde:
            omitidas.append({"num_factura": num, "motivo": "fuera_ventana_desde"})
            continue
        if hasta and f_dt and f_dt > hasta:
            omitidas.append({"num_factura": num, "motivo": "fuera_ventana_hasta"})
            continue

        sri_rows = (
            sb.table("sri_comprobantes_recibidos")
            .select("clave_acceso,estado,xml_autorizado,num_factura,ruc_emisor")
            .eq("num_factura", num)
            .eq("ruc_emisor", ruc)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not sri_rows:
            omitidas.append({"num_factura": num, "motivo": "sin_xml_sri"})
            continue
        sri = sri_rows[0]
        xml = (sri.get("xml_autorizado") or "").strip()
        if not xml:
            omitidas.append({"num_factura": num, "motivo": "xml_vacio"})
            continue

        factura = parsear_xml_sri(xml)
        if not factura:
            omitidas.append({"num_factura": num, "motivo": "parseo_xml_fallo"})
            continue

        lineas: dict[str, list[str]] = {}
        for item in factura.get("items") or []:
            cls = _clasificar_linea(
                factura,
                item,
                pendientes_estado=pendientes_estado,
                solo_promovidos=solo_promovidos,
            )
            lineas.setdefault(cls, []).append(
                (item.get("cod_item_xml") or "")[:20]
                + " "
                + (item.get("descripcion_proveedor") or "")[:30]
            )
            conteo_lineas[cls] = conteo_lineas.get(cls, 0) + 1

        nuevas = len(lineas.get("nueva_entrada", []))
        if nuevas == 0:
            motivo = "sin_lineas_nuevas"
            if lineas.get("ya_ingresado"):
                motivo = "todo_ya_ingresado"
            elif lineas.get("pendiente_sin_promover"):
                motivo = "pendiente_sin_promover"
            elif lineas.get("sin_catalogo") and not lineas.get("ya_ingresado"):
                motivo = "sin_catalogo"
            omitidas.append({"num_factura": num, "motivo": motivo, "lineas": lineas})
            continue

        elegibles.append(
            {
                "num_factura": num,
                "ruc": ruc,
                "fecha": f_raw,
                "clave_acceso": sri["clave_acceso"],
                "estado_sri": sri.get("estado"),
                "lineas_nuevas": nuevas,
                "lineas_ya_ingresadas": len(lineas.get("ya_ingresado", [])),
                "detalle_nuevas": lineas.get("nueva_entrada", [])[:5],
            }
        )

    return {
        "elegibles": elegibles,
        "omitidas": omitidas,
        "conteo_lineas_global": conteo_lineas,
        "total_parcial_consultadas": len(parciales),
    }


def reencolar_claves(sb, claves: list[str], *, dry_run: bool) -> int:
    n = 0
    for clave in claves:
        if dry_run:
            print(f"  [DRY RUN] reencolar {clave[:12]}... -> DESCARGADO")
            n += 1
            continue
        existente = (
            sb.table("sri_comprobantes_recibidos")
            .select("meta")
            .eq("clave_acceso", clave)
            .limit(1)
            .execute()
            .data
            or [{}]
        )[0]
        meta = dict(existente.get("meta") or {})
        meta["reproceso_post_promocion"] = datetime.now().isoformat()
        sb.table("sri_comprobantes_recibidos").update(
            {
                "estado": "DESCARGADO",
                "fecha_proceso": None,
                "meta": meta,
            }
        ).eq("clave_acceso", clave).execute()
        print(f"  OK reencolado {clave[:12]}... -> DESCARGADO")
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Solo análisis; no escribe")
    parser.add_argument("--desde", metavar="YYYY-MM-DD", help="Filtro fecha factura mínima")
    parser.add_argument("--hasta", metavar="YYYY-MM-DD", help="Filtro fecha factura máxima")
    parser.add_argument(
        "--incluir-sin-fila-pendientes",
        action="store_true",
        help="Incluir líneas con catálogo aunque no estén REGISTRADO en pendientes",
    )
    parser.add_argument(
        "--recalcular",
        action="store_true",
        help="Tras reprocesar, ejecutar recalcular_stock_sheets --produccion",
    )
    parser.add_argument("--limite", type=int, default=500, help="Máx facturas PARCIAL a evaluar")
    args = parser.parse_args(argv)

    desde = _parse_fecha(args.desde)
    hasta = _parse_fecha(args.hasta)
    solo_promovidos = not args.incluir_sin_fila_pendientes

    print("=" * 60)
    print("REPROCESO PARCIALES SRI (post-promoción catálogo)")
    print("=" * 60)
    print("Salvaguardas:")
    print("  - Solo facturas con estado PARCIAL (nunca COMPLETA)")
    print("  - Solo si hay líneas SIN entrada previa en mov_inventario")
    print("  - Líneas ya ingresadas: solo precios, no duplica stock")
    print("  - recalcular_stock_sheets suma mov_inventario (sin doble conteo)")
    if solo_promovidos:
        print("  - Solo líneas con pendiente REGISTRADO (tras promover)")
    if desde or hasta:
        print(f"  - Ventana factura: {desde or '...'} .. {hasta or '...'}")
    print()

    analisis = analizar_parciales_elegibles(
        desde=desde,
        hasta=hasta,
        solo_promovidos=solo_promovidos,
        limite=args.limite,
    )
    elegibles = analisis["elegibles"]
    omitidas = analisis["omitidas"]

    print(
        f"PARCIAL consultadas: {analisis['total_parcial_consultadas']} | "
        f"elegibles reproceso: {len(elegibles)} | omitidas: {len(omitidas)}"
    )
    if analisis["conteo_lineas_global"]:
        print("Clasificación líneas (global):")
        for k, v in sorted(analisis["conteo_lineas_global"].items()):
            print(f"  {k}: {v}")

    if elegibles:
        print("\nFacturas a reprocesar (tendrán nuevas ENTRADAS):")
        for e in elegibles[:25]:
            print(
                f"  {e['num_factura']} ({e['fecha']}) | "
                f"+{e['lineas_nuevas']} línea(s) nueva(s) | "
                f"{e['lineas_ya_ingresadas']} ya ingresada(s)"
            )
            for d in e.get("detalle_nuevas") or []:
                print(f"      · {d.strip()}")
        if len(elegibles) > 25:
            print(f"  ... y {len(elegibles) - 25} más")

    motivos = {}
    for o in omitidas:
        m = o.get("motivo", "?")
        motivos[m] = motivos.get(m, 0) + 1
    if motivos:
        print("\nOmitidas por motivo:")
        for m, c in sorted(motivos.items(), key=lambda x: -x[1]):
            print(f"  {m}: {c}")

    if not elegibles:
        print("\nNada que reprocesar.")
        return 0

    if args.dry_run:
        print("\n[DRY RUN] No se reencola ni se procesa.")
        return 0

    sb = _supabase()
    claves = [e["clave_acceso"] for e in elegibles]
    print(f"\nReencolando {len(claves)} comprobante(s)...")
    reencolar_claves(sb, claves, dry_run=False)

    from procesar_facturas_sri import fase_proceso
    from sri_client import SriConfig

    config = SriConfig.from_env()
    print("\nEjecutando fase proceso SRI (solo cola DESCARGADO)...")
    res_proc = fase_proceso(config, "REPROCESO_PROMO", dry_run=False)
    print(f"Resumen proceso: {res_proc}")

    if args.recalcular:
        print("\nRecalculando stock desde mov_inventario...")
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent
        py = root / "venv" / "Scripts" / "python.exe"
        if not py.is_file():
            py = Path(sys.executable)
        r = subprocess.run(
            [str(py), str(root / "recalcular_stock_sheets.py"), "--produccion"],
            cwd=str(root),
        )
        if r.returncode != 0:
            print(f"WARN: recalcular_stock terminó con código {r.returncode}")
            return r.returncode

    print("\nReproceso post-promoción finalizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

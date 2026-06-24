"""
Backfill de costos históricos desde facturas SRI/Drive sin modificar stock.

Inserta mov_inventario tipo ENTRADA_COSTO_HIST (cantidad_mov=0, costo real).
No escribe stock en BD_MP_SISTEMA. No bloquea futuras ENTRADAs con stock.

Fuentes (--fuente):
  supabase  XML ya descargados en sri_comprobantes_recibidos
  drive     XML en carpeta Google Drive (GOOGLE_DRIVE_FACTURAS_FOLDER_ID)
  sri       Descarga portal SRI (--desde/--hasta) y luego procesa desde Supabase

Uso recomendado:
  # 1) Simular enero-mayo (solo facturas anteriores al corte de stock)
  python backfill_costos_facturas_historicas.py --dry-run --fuente supabase \\
    --desde 2026-01-01 --hasta 2026-05-28

  # 2) Descargar del SRI y simular
  python backfill_costos_facturas_historicas.py --dry-run --fuente sri \\
    --desde 2026-01-01 --hasta 2026-05-28

  # 3) Ejecutar (verifica stock antes/después)
  python backfill_costos_facturas_historicas.py --produccion --fuente supabase \\
    --desde 2026-01-01 --hasta 2026-05-28

  # 4) Huecos recientes sin ENTRADA (solo costo, cantidad=0)
  python backfill_costos_facturas_historicas.py --dry-run --fuente drive \\
    --desde 2026-05-29 --hasta 2026-06-15 --incluir-huecos-recientes
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

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


def _en_rango(fecha_iso: str, desde: date, hasta: date) -> bool:
    f = _parse_fecha(fecha_iso)
    if not f:
        return False
    return desde <= f <= hasta


@dataclass
class StatsBackfill:
    facturas_leidas: int = 0
    facturas_en_rango: int = 0
    lineas_total: int = 0
    insertadas: int = 0
    skips: Counter = field(default_factory=Counter)
    detalle: list[dict] = field(default_factory=list)

    def skip(self, razon: str, **extra) -> None:
        self.skips[razon] += 1
        if extra:
            self.detalle.append({"accion": "SKIP", "razon": razon, **extra})

    def ok(self, **extra) -> None:
        self.insertadas += 1
        self.detalle.append({"accion": "INSERT", **extra})


def _snapshot_stock() -> dict[tuple[str, str], float]:
    from recalcular_stock_sheets import build_stock_calculado

    return build_stock_calculado()


def _verificar_stock_igual(antes: dict, despues: dict) -> list[str]:
    errores: list[str] = []
    keys = set(antes) | set(despues)
    for k in sorted(keys):
        a = round(float(antes.get(k, 0)), 6)
        b = round(float(despues.get(k, 0)), 6)
        if a != b:
            errores.append(f"  {k[0]}@{k[1]}: {a} -> {b}")
    return errores


def _iter_facturas_supabase(sb, desde: date, hasta: date) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("sri_comprobantes_recibidos")
            .select("clave_acceso,num_factura,ruc_emisor,fecha_emision,xml_autorizado,estado")
            .gte("fecha_emision", desde.isoformat())
            .lte("fecha_emision", hasta.isoformat())
            .order("fecha_emision")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def _iter_facturas_drive(desde: date, hasta: date, *, limite: int = 0) -> list[dict]:
    from procesar_facturas_drive import descargar_xml, listar_xmls_pendientes, parsear_xml_sri

    out: list[dict] = []
    for archivo in listar_xmls_pendientes():
        if limite and len(out) >= limite:
            break
        try:
            texto = descargar_xml(archivo["id"])
        except Exception as e:
            print(f"  WARN descarga {archivo.get('name')}: {e}")
            continue
        factura = parsear_xml_sri(texto)
        if not factura:
            continue
        if not _en_rango(factura.get("fecha_factura", ""), desde, hasta):
            continue
        out.append(
            {
                "fuente": "drive",
                "file_id": archivo.get("id", ""),
                "file_name": archivo.get("name", ""),
                "xml": texto,
                "factura": factura,
            }
        )
    return out


def _descargar_sri(desde: date, hasta: date, *, dry_run: bool) -> dict:
    from procesar_facturas_sri import fase_descarga
    from sri_client import SriConfig

    config = SriConfig.from_env()
    faltantes = config.validar()
    if faltantes:
        raise RuntimeError(f"Faltan variables SRI en .env: {', '.join(faltantes)}")
    return fase_descarga(config, "BACKFILL", dry_run, desde, hasta)


def _tokens_descripcion(texto: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9]{3,}", (texto or "").upper()))


def buscar_item_prov_aproximado(
    factura: dict,
    item: dict,
) -> tuple[dict | None, str]:
    """
    Match exacto → fuzzy por tokens en catálogo del mismo proveedor.
    Retorna (item_prov, metodo) con metodo: exacto | fuzzy | vacío.
    """
    from codigo_factura_match import normalizar_cod_proveedor_para_match
    from procesar_facturas_drive import (
        _cod_proveedor_desde_ruc,
        buscar_item_prov,
        cargar_bd_items_prov,
        cargar_lookup_ruc,
    )

    ruc = str(factura.get("ruc") or "").strip()
    num_f = str(factura.get("num_factura") or "").strip()
    desc = str(item.get("descripcion_proveedor") or "").strip()

    exact = buscar_item_prov(
        ruc,
        item.get("cod_item_xml", ""),
        desc,
        factura.get("razon_social", ""),
        num_f,
    )
    if exact:
        return exact, "exacto"

    desc_t = _tokens_descripcion(desc)
    if len(desc_t) < 2:
        return None, ""

    lookup = cargar_lookup_ruc()
    cod_prov = _cod_proveedor_desde_ruc(lookup, ruc, num_f)
    cod_prov_n = normalizar_cod_proveedor_para_match(cod_prov)

    best: dict | None = None
    best_score = 0.0
    for row in cargar_bd_items_prov():
        if cod_prov_n and normalizar_cod_proveedor_para_match(row.get("cod_proveedor", "")) != cod_prov_n:
            continue
        cat_t = _tokens_descripcion(
            f"{row.get('descripcion_proveedor', '')} {row.get('nombre_mp', '')}"
        )
        if not cat_t:
            continue
        overlap = len(desc_t & cat_t)
        if overlap < 2:
            continue
        score = overlap / max(len(desc_t), len(cat_t), 1)
        if score > best_score:
            best_score = score
            best = row

    if best and best_score >= 0.15:
        return best, "fuzzy"
    return None, ""


_bodega_por_ruc_cache: dict[str, str] | None = None


_SERVICIO_NO_INVENTARIO = frozenset(
    {
        "GOPYMES",
        "GOMAX",
        "INTERNET",
        "MBPS",
        "SERVICIO",
        "SERVICIOS",
        "ARRIENDO",
        "TELEFON",
        "TELECOM",
        "LUZ",
        "ELECTRIC",
        "AGUA",
        "HONORARIO",
        "CONTADOR",
        "SOFTWARE",
        "LICENCIA",
        "PLAN FAMILY",
    }
)


def _es_linea_servicio_no_inventario(desc: str) -> bool:
    u = (desc or "").upper()
    return any(k in u for k in _SERVICIO_NO_INVENTARIO)


def _bodega_desde_ruc_proveedor(ruc: str) -> tuple[str | None, bool]:
    """Retorna (bodega, es_fallback). es_fallback=True si no está en BD_PROV inventario."""
    global _bodega_por_ruc_cache
    from bodegas_config import normalizar_cod_bodega
    from dashboard_services.compras import _ruc_claves

    if _bodega_por_ruc_cache is None:
        from procesar_facturas_drive import _get_sheet

        lookup: dict[str, str] = {}
        try:
            ws = _get_sheet().worksheet("BD_PROV")
            vals = ws.get_all_values()
            hi = next(
                (i for i, r in enumerate(vals) if any((c or "").strip() == "cod_proveedor" for c in r)),
                None,
            )
            if hi is not None:
                headers = [(c or "").strip() for c in vals[hi]]
                ir = headers.index("ruc") if "ruc" in headers else headers.index("RUC")
                it = headers.index("Tipo") if "Tipo" in headers else headers.index("tipo")
                for row in vals[hi + 1 :]:
                    if not any((c or "").strip() for c in row):
                        continue
                    ruc_row = (row[ir] if ir < len(row) else "").strip()
                    tipo = (row[it] if it < len(row) else "").strip().upper()
                    bod = "BOD-002" if tipo == "BARRA" else "BOD-001" if tipo == "COCINA" else ""
                    if not bod:
                        continue
                    for key in _ruc_claves(ruc_row):
                        lookup[key] = bod
        except Exception as e:
            print(f"  WARN cargando bodegas BD_PROV: {e}")
        _bodega_por_ruc_cache = lookup

    for key in _ruc_claves(ruc):
        bod = _bodega_por_ruc_cache.get(key)
        if bod:
            return normalizar_cod_bodega(bod), False
    return None, True


def _procesar_linea(
    factura: dict,
    item: dict,
    stats: StatsBackfill,
    *,
    dry_run: bool,
    solo_pre_cutoff: bool,
    incluir_huecos_recientes: bool,
    actualizar_precios: bool,
    fecha_min_stock: str,
) -> None:
    from procesar_facturas_drive import (
        conversion_compra_definida,
        mov_costo_historico_linea_ya_registrada,
        mov_entrada_factura_linea_ya_registrada,
        procesar_variacion_precio,
        registrar_entrada_costo_historico,
        registrar_entrada_costo_historico_sin_catalogo,
    )

    num_f = (factura.get("num_factura") or "").strip()
    fecha_f = (factura.get("fecha_factura") or "").strip()[:10]
    es_pre_cutoff = fecha_f < fecha_min_stock

    if solo_pre_cutoff and not es_pre_cutoff:
        stats.skip("fuera_rango_post_cutoff", num_factura=num_f, fecha=fecha_f)
        return

    if mov_costo_historico_linea_ya_registrada(num_f, item):
        stats.skip("ya_backfill_linea", num_factura=num_f, cod_xml=item.get("cod_item_xml", ""))
        return

    costo_total = round(float(item.get("precio_total_sin_impuesto") or 0), 4)
    if costo_total <= 0:
        stats.skip("costo_total_cero", num_factura=num_f)
        return

    item_prov, metodo = buscar_item_prov_aproximado(factura, item)

    if item_prov:
        cod_mp = (item_prov.get("cod_mp_sistema") or "").strip()
        if mov_entrada_factura_linea_ya_registrada(num_f, cod_mp or "000", item):
            stats.skip("ya_entrada_con_stock", num_factura=num_f, cod_mp=cod_mp or "?")
            return

        ok_conv, _ = conversion_compra_definida(item_prov)
        aproximado = metodo == "fuzzy" or not ok_conv

        if actualizar_precios and not dry_run and metodo == "exacto" and ok_conv:
            procesar_variacion_precio(
                item_prov, factura, item, solo_escritura_precio=True
            )

        ok = registrar_entrada_costo_historico(
            item_prov,
            item,
            factura,
            dry_run=dry_run,
            aproximado=aproximado,
        )
        if ok:
            stats.ok(
                num_factura=num_f,
                fecha=fecha_f,
                cod_mp=cod_mp or "000",
                costo_total=costo_total,
                metodo=metodo + ("_aprox" if aproximado else ""),
                desc=item.get("descripcion_proveedor", "")[:60],
            )
        else:
            stats.skip("error_registro", num_factura=num_f, cod_mp=cod_mp or "?")
        return

    bodega, bodega_fallback = _bodega_desde_ruc_proveedor(str(factura.get("ruc") or ""))
    desc = str(item.get("descripcion_proveedor") or "")

    if _es_linea_servicio_no_inventario(desc):
        stats.skip(
            "excluido_servicio",
            num_factura=num_f,
            desc=desc[:60],
        )
        return

    if not bodega:
        from bodegas_config import normalizar_cod_bodega

        bodega = normalizar_cod_bodega("BOD-001")

    ok = registrar_entrada_costo_historico_sin_catalogo(
        item,
        factura,
        cod_bodega_destino=bodega,
        dry_run=dry_run,
    )
    if ok:
        stats.ok(
            num_factura=num_f,
            fecha=fecha_f,
            cod_mp="000",
            costo_total=costo_total,
            metodo="sin_catalogo" + ("_bodega_def" if bodega_fallback else ""),
            desc=item.get("descripcion_proveedor", "")[:60],
        )
    else:
        stats.skip("error_registro_sin_catalogo", num_factura=num_f)


def _procesar_factura_dict(
    factura: dict,
    stats: StatsBackfill,
    *,
    dry_run: bool,
    solo_pre_cutoff: bool,
    incluir_huecos_recientes: bool,
    actualizar_precios: bool,
    fecha_min_stock: str,
) -> None:
    stats.facturas_leidas += 1
    fecha_f = (factura.get("fecha_factura") or "").strip()[:10]
    if not fecha_f:
        stats.skip("factura_sin_fecha", num_factura=factura.get("num_factura", ""))
        return
    stats.facturas_en_rango += 1

    for item in factura.get("items") or []:
        stats.lineas_total += 1
        _procesar_linea(
            factura,
            item,
            stats,
            dry_run=dry_run,
            solo_pre_cutoff=solo_pre_cutoff,
            incluir_huecos_recientes=incluir_huecos_recientes,
            actualizar_precios=actualizar_precios,
            fecha_min_stock=fecha_min_stock,
        )


def _procesar_desde_supabase(
    sb,
    desde: date,
    hasta: date,
    stats: StatsBackfill,
    *,
    dry_run: bool,
    solo_pre_cutoff: bool,
    incluir_huecos_recientes: bool,
    actualizar_precios: bool,
    fecha_min_stock: str,
    limite: int,
) -> None:
    from procesar_facturas_drive import parsear_xml_sri

    rows = _iter_facturas_supabase(sb, desde, hasta)
    print(f"Registros SRI en Supabase ({desde}..{hasta}): {len(rows)}")
    n = 0
    for row in rows:
        if limite and n >= limite:
            break
        xml = (row.get("xml_autorizado") or "").strip()
        if not xml:
            stats.skip("sin_xml", clave=(row.get("clave_acceso") or "")[:12])
            continue
        factura = parsear_xml_sri(xml)
        if not factura:
            stats.skip("parseo_fallido", num=(row.get("num_factura") or ""))
            continue
        n += 1
        print(
            f"\n[{n}] {factura.get('num_factura')} | {factura.get('fecha_factura')} | "
            f"{factura.get('razon_social', '')[:40]}"
        )
        _procesar_factura_dict(
            factura,
            stats,
            dry_run=dry_run,
            solo_pre_cutoff=solo_pre_cutoff,
            incluir_huecos_recientes=incluir_huecos_recientes,
            actualizar_precios=actualizar_precios,
            fecha_min_stock=fecha_min_stock,
        )


def _procesar_desde_drive(
    desde: date,
    hasta: date,
    stats: StatsBackfill,
    *,
    dry_run: bool,
    solo_pre_cutoff: bool,
    incluir_huecos_recientes: bool,
    actualizar_precios: bool,
    fecha_min_stock: str,
    limite: int,
) -> None:
    from procesar_facturas_drive import cargar_bd_items_prov

    cargar_bd_items_prov()
    packs = _iter_facturas_drive(desde, hasta, limite=limite)
    print(f"XML en Drive en rango: {len(packs)}")
    for i, pack in enumerate(packs, 1):
        factura = pack["factura"]
        print(
            f"\n[{i}] {factura.get('num_factura')} | {factura.get('fecha_factura')} | "
            f"{pack.get('file_name', '')[:50]}"
        )
        _procesar_factura_dict(
            factura,
            stats,
            dry_run=dry_run,
            solo_pre_cutoff=solo_pre_cutoff,
            incluir_huecos_recientes=incluir_huecos_recientes,
            actualizar_precios=actualizar_precios,
            fecha_min_stock=fecha_min_stock,
        )


def _exportar_reporte(stats: StatsBackfill, *, tag: str, desde: date, hasta: date) -> Path:
    out_dir = Path(__file__).resolve().parent / "exports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"backfill_costos_{desde}_{hasta}_{tag}_{ts}.csv"
    fields = sorted({k for row in stats.detalle for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(stats.detalle)
    return path


def _imprimir_resumen(stats: StatsBackfill, *, tag: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"RESUMEN BACKFILL COSTOS [{tag}]")
    print(f"{'=' * 60}")
    print(f"  Facturas procesadas : {stats.facturas_en_rango} / {stats.facturas_leidas}")
    print(f"  Líneas XML          : {stats.lineas_total}")
    print(f"  ENTRADA_COSTO_HIST  : {stats.insertadas}")
    if stats.skips:
        print("  Omitidas:")
        for razon, cnt in stats.skips.most_common():
            print(f"    - {razon}: {cnt}")


def main(argv: list[str] | None = None) -> int:
    from procesar_facturas_drive import FECHA_MIN_INGRESO_FACTURA, cargar_bd_items_prov

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true", help="Simular sin escribir en Supabase")
    p.add_argument("--produccion", action="store_true", help="Insertar ENTRADA_COSTO_HIST")
    p.add_argument(
        "--fuente",
        choices=("supabase", "drive", "sri"),
        default="supabase",
        help="Origen de XML (default: supabase)",
    )
    p.add_argument("--desde", required=True, help="Fecha inicio YYYY-MM-DD")
    p.add_argument("--hasta", required=True, help="Fecha fin YYYY-MM-DD")
    p.add_argument(
        "--incluir-huecos-recientes",
        action="store_true",
        help=(
            "También procesar facturas >= TATAMI_FECHA_MIN_INGRESO_FACTURA "
            "si la línea no tiene ENTRADA con stock"
        ),
    )
    p.add_argument(
        "--solo-pre-cutoff",
        action="store_true",
        help=f"Solo facturas anteriores a {FECHA_MIN_INGRESO_FACTURA} (default implícito sin --incluir-huecos-recientes)",
    )
    p.add_argument(
        "--actualizar-precios",
        action="store_true",
        help="Actualizar precio_ref en BD_ITEMS_PROV (solo_escritura_precio, sin hist_precios)",
    )
    p.add_argument("--limite", type=int, default=0, help="Máximo facturas a procesar (0=todas)")
    p.add_argument(
        "--skip-verificacion-stock",
        action="store_true",
        help="No comparar stock calculado antes/después (solo --produccion)",
    )
    args = p.parse_args(argv)

    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2
    if args.dry_run and args.produccion:
        print("Usa solo uno: --dry-run o --produccion")
        return 2

    desde = _parse_fecha(args.desde)
    hasta = _parse_fecha(args.hasta)
    if not desde or not hasta:
        print("ERROR: --desde y --hasta deben ser YYYY-MM-DD")
        return 1
    if desde > hasta:
        print("ERROR: --desde no puede ser posterior a --hasta")
        return 1

    fecha_min_stock = FECHA_MIN_INGRESO_FACTURA
    solo_pre_cutoff = args.solo_pre_cutoff or not args.incluir_huecos_recientes
    dry_run = args.dry_run
    tag = "DRY-RUN" if dry_run else "PRODUCCION"

    print("=" * 60)
    print(f"Backfill costos históricos | {tag}")
    print(f"Fuente: {args.fuente} | Rango: {desde} .. {hasta}")
    print(f"Corte stock (sin ENTRADA normal): {fecha_min_stock}")
    print(f"Modo: {'solo pre-cutoff' if solo_pre_cutoff else 'pre-cutoff + huecos recientes'}")
    print(f"tipo_mov: ENTRADA_COSTO_HIST (cantidad_mov=0, no afecta stock)")
    if args.actualizar_precios:
        print("También: actualizar precio_ref en BD_ITEMS_PROV")
    print("=" * 60)

    cargar_bd_items_prov()
    stats = StatsBackfill()
    stock_antes: dict | None = None

    if args.produccion and not args.skip_verificacion_stock:
        print("\nSnapshot stock (mov_inventario) antes del backfill...")
        stock_antes = _snapshot_stock()
        print(f"  Claves MP×bodega: {len(stock_antes)}")

    if args.fuente == "sri":
        print("\n--- Fase descarga SRI ---")
        res_desc = _descargar_sri(desde, hasta, dry_run=dry_run)
        print(f"Descarga: {res_desc}")
        args.fuente = "supabase"

    if args.fuente == "supabase":
        from supabase import create_client

        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        _procesar_desde_supabase(
            sb,
            desde,
            hasta,
            stats,
            dry_run=dry_run,
            solo_pre_cutoff=solo_pre_cutoff,
            incluir_huecos_recientes=args.incluir_huecos_recientes,
            actualizar_precios=args.actualizar_precios,
            fecha_min_stock=fecha_min_stock,
            limite=args.limite,
        )
    elif args.fuente == "drive":
        _procesar_desde_drive(
            desde,
            hasta,
            stats,
            dry_run=dry_run,
            solo_pre_cutoff=solo_pre_cutoff,
            incluir_huecos_recientes=args.incluir_huecos_recientes,
            actualizar_precios=args.actualizar_precios,
            fecha_min_stock=fecha_min_stock,
            limite=args.limite,
        )

    _imprimir_resumen(stats, tag=tag)

    if stats.detalle:
        reporte = _exportar_reporte(stats, tag=tag.lower(), desde=desde, hasta=hasta)
        print(f"\nReporte CSV: {reporte}")

    if args.produccion and stock_antes is not None and not args.skip_verificacion_stock:
        print("\nVerificando stock calculado después del backfill...")
        stock_despues = _snapshot_stock()
        diffs = _verificar_stock_igual(stock_antes, stock_despues)
        if diffs:
            print("ERROR: el stock calculado cambió (no debería ocurrir):")
            for line in diffs[:30]:
                print(line)
            if len(diffs) > 30:
                print(f"  ... y {len(diffs) - 30} más")
            return 3
        print("  OK: stock calculado idéntico antes/después.")

    if stats.insertadas and not dry_run:
        print(
            "\nSiguiente paso sugerido: revisar dashboard rentabilidad/compras "
            f"para {desde}..{hasta}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

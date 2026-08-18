"""
Audita ENTRADAs duplicadas por factura (mismo MP + ITEM_XML equivalente).

Modo barra post-conteo: solo BOD-002 con movimientos DESPUÉS del contabilizado
del conteo físico 29-may-2026. Pre-conteo no se corrige: la foto del 29-may
fijó baseline.

Uso:
  python auditar_entradas_factura_duplicadas.py --post-conteo-barra
  python auditar_entradas_factura_duplicadas.py --desde 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(override=True)

from codigo_factura_match import normalizar_cod_item_para_match
from ventas_documento_prefactura import FECHA_CONTEO
from whatsapp_webhook import conectar_supabase

ITEM_XML_RE = re.compile(r"ITEM_XML:([^\s|]+)")
BOD_BARRA = "BOD-002"
CONTEO_BARRA_ENVIO_ID = "d9663c82-c337-4c61-83e0-8eb99fb589f4"
CONTEO_BARRA_CICLO_ID = "4d65003c-1dab-4a7d-90e7-a4b6b5a62fb8"
FALLBACK_CORTE_POST_CONTEO = f"{FECHA_CONTEO}T18:47:32"


def extraer_item_xml(obs: str) -> str:
    m = ITEM_XML_RE.search(obs or "")
    return m.group(1).strip() if m else ""


def ts_insercion_cod_mov(cod_mov: str) -> str:
    parts = (cod_mov or "").split("-")
    if len(parts) >= 4:
        t = parts[-1]
        if len(t) >= 14 and t[:14].isdigit():
            return t[:14]
    return ""


def corte_post_conteo_barra(sb) -> tuple[str, dict]:
    envio = (
        sb.table("conteo_envio")
        .select("contabilizado_at,enviado_at")
        .eq("id", CONTEO_BARRA_ENVIO_ID)
        .single()
        .execute()
        .data
        or {}
    )
    ciclo = (
        sb.table("conteo_ciclo")
        .select("snapshot_at,estado")
        .eq("id", CONTEO_BARRA_CICLO_ID)
        .single()
        .execute()
        .data
        or {}
    )
    cutoff = (
        envio.get("contabilizado_at") or envio.get("enviado_at") or FALLBACK_CORTE_POST_CONTEO
    )[:19]
    meta = {**envio, **ciclo, "corte": cutoff}
    return cutoff, meta


def cargar_entradas(sb, *, desde: str, bodega: str | None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = (
            sb.table("mov_inventario")
            .select(
                "cod_mov,fecha,num_documento,cod_mp_sistema,nombre_mp,cantidad_mov,"
                "costo_total,cod_bodega_destino,observaciones"
            )
            .eq("tipo_mov", "ENTRADA")
            .eq("origen_documento", "FACTURA")
            .gte("fecha", desde)
            .order("fecha")
        )
        if bodega:
            q = q.eq("cod_bodega_destino", bodega)
        chunk = q.range(offset, offset + 999).execute().data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def analizar_duplicados(rows: list[dict]) -> tuple[list[dict], list]:
    grupos: dict[tuple, list[dict]] = defaultdict(list)
    sin_xml: dict[tuple, list[dict]] = defaultdict(list)

    for r in rows:
        doc = (r.get("num_documento") or "").strip()
        mp = (r.get("cod_mp_sistema") or "").strip()
        xml = extraer_item_xml(r.get("observaciones") or "")
        if xml:
            key = (doc, mp, normalizar_cod_item_para_match(xml))
            grupos[key].append({**r, "_item_xml_raw": xml})
        else:
            key2 = (doc, mp, float(r.get("cantidad_mov") or 0), float(r.get("costo_total") or 0))
            sin_xml[key2].append(r)

    informe: list[dict] = []
    for (doc, mp, xml_norm), movs in grupos.items():
        if len(movs) < 2:
            continue
        movs_sorted = sorted(
            movs,
            key=lambda m: ts_insercion_cod_mov(m.get("cod_mov") or "") or m.get("fecha") or "",
        )
        keep = movs_sorted[0]
        informe.append(
            {
                "factura": doc,
                "cod_mp": mp,
                "item_xml_norm": xml_norm,
                "cantidad": float(keep.get("cantidad_mov") or 0),
                "conservar": keep["cod_mov"],
                "duplicados": [m["cod_mov"] for m in movs_sorted[1:]],
                "detalle": [
                    {
                        "cod_mov": m["cod_mov"],
                        "fecha": (m.get("fecha") or "")[:19],
                        "ts_insercion": ts_insercion_cod_mov(m.get("cod_mov") or ""),
                        "cantidad": float(m.get("cantidad_mov") or 0),
                        "costo_total": float(m.get("costo_total") or 0),
                        "item_xml_raw": m.get("_item_xml_raw", ""),
                        "accion": "CONSERVAR" if m is keep else "REVERTIR",
                    }
                    for m in movs_sorted
                ],
            }
        )

    informe.sort(key=lambda x: (x["factura"], x["cod_mp"]))
    dupes_sin = [(k, v) for k, v in sin_xml.items() if len(v) > 1]
    return informe, dupes_sin


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desde", default="2026-01-01")
    p.add_argument("--post-conteo-barra", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    sb = conectar_supabase()
    bodega = BOD_BARRA if args.post_conteo_barra else None
    desde = FECHA_CONTEO if args.post_conteo_barra else args.desde
    cutoff = ""
    meta: dict = {}

    if args.post_conteo_barra:
        cutoff, meta = corte_post_conteo_barra(sb)

    rows = cargar_entradas(sb, desde=desde, bodega=bodega)
    if cutoff:
        rows = [r for r in rows if (r.get("fecha") or "")[:19] > cutoff]

    informe, dupes_sin = analizar_duplicados(rows)

    if args.json:
        print(
            json.dumps(
                {
                    "corte_post_conteo": cutoff,
                    "meta_conteo": meta,
                    "entradas_analizadas": len(rows),
                    "grupos_duplicados": informe,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    titulo = "POST-CONTEO BARRA" if args.post_conteo_barra else "GENERAL"
    print(f"=== Auditoría duplicados ENTRADA ({titulo}) ===")
    if args.post_conteo_barra:
        print(f"Conteo barra {FECHA_CONTEO} | snapshot {str(meta.get('snapshot_at',''))[:19]}")
        print(f"  enviado={str(meta.get('enviado_at',''))[:19]} contabilizado={str(meta.get('contabilizado_at',''))[:19]}")
        print(f"  Solo ENTRADAs BOD-002 con fecha_mov > {cutoff}")
    print(f"Entradas analizadas: {len(rows)} | ítems duplicados: {len(informe)}\n")

    total_qty = total_usd = 0.0
    for g in informe:
        print(
            f"FACT {g['factura']} | MP {g['cod_mp']} | xml={g['item_xml_norm']} | qty={g['cantidad']:g}"
        )
        for d in g["detalle"]:
            mark = "KEEP " if d["accion"] == "CONSERVAR" else "DEL  "
            print(
                f"  {mark}{d['cod_mov']} | ins={d['ts_insercion']} | "
                f"{d['item_xml_raw']} | +{d['cantidad']:g} | ${d['costo_total']:,.2f}"
            )
            if d["accion"] == "REVERTIR":
                total_qty += d["cantidad"]
                total_usd += d["costo_total"]
        print()

    if dupes_sin and not args.post_conteo_barra:
        print(f"--- Sin ITEM_XML ({len(dupes_sin)} grupos pre/post conteo mixto) ---")

    n_rev = sum(len(g["duplicados"]) for g in informe)
    print(f"RESUMEN: revertir {n_rev} mov | inflado ~{total_qty:,.0f} uni | ${total_usd:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

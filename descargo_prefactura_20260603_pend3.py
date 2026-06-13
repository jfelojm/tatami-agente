"""
Descargo manual de inventario — prefactura PENDIENTES #3 (2026-06-03).

Facturada como PUBLICIDAD Y PROPAGANDA (doc 6395, sin descarga_inventario).
Este script descuenta solo los platos de la prefactura impresa.

Uso:
  python descargo_prefactura_20260603_pend3.py
  python descargo_prefactura_20260603_pend3.py --produccion
  python descargo_prefactura_20260603_pend3.py --produccion --recalcular-stock
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent

# Referencia única (idempotencia)
REF = "PREF-20260603-PEND3"
FECHA = "2026-06-03"
HORA = "12:31:00"
DOC_SMART = "6395"

# Platos prefactura: (cod_smart_menu, detalle_plato Smart Menu, cantidad)
LINEAS_PREFACTURA: list[tuple[str, str, float]] = [
    ("1", "DUMPLINGS (5 UNID) RES", 1),
    ("27", "AGUA DE VIDRIO AGUA CON GAS", 1),
    ("5", "BAO AKARUI (PANCETA)", 1),
    ("5", "BAO CRISPY (POLLO)", 1),
    ("5", "BAO EBI ZEN (PANKO)", 1),
    ("5", "BAO GOCHU (POLLO PICANTE)", 1),
    ("9", "DRUNKEN NOODLES", 1),
    ("1", "DUMPLINGS (5 UNID) CERDO", 1),
    ("1", "DUMPLINGS (5 UNID) RES", 1),
    ("12", "LOMO KURO", 2),
    ("10", "PAD THAI", 1),
    ("2", "PAPAS FURAI", 1),
    ("14", "TAMAGO RICE", 1),
]


def _ya_descargado(sb) -> bool:
    rows = (
        sb.table("mov_inventario")
        .select("cod_mov")
        .eq("num_documento", REF)
        .limit(1)
        .execute()
        .data
        or []
    )
    return bool(rows)


def _construir_movimientos() -> tuple[list[dict], list[str]]:
    from bodegas_config import resolver_bodega_receta
    from descargo_inventario import (
        _iso_fecha_hora_mov,
        _sheet_float,
        calcular_consumo,
        cargar_mp_sistema,
        get_ingredientes,
    )
    from descargo_subreceta import (
        cargar_metadata_subrecetas,
        descargo_subrecetas_habilitado,
        preparar_ingredientes_descargo,
        procesar_linea_sub_venta,
    )
    from matching_productos import cargar_bd_productos, construir_lookup, resolver_match

    lookup = construir_lookup(cargar_bd_productos())
    mp_sistema = cargar_mp_sistema()
    incluir_sub = descargo_subrecetas_habilitado()
    subs_meta = cargar_metadata_subrecetas() if incluir_sub else {}

    def _mp_key(cod_mp: str, bodega: str) -> tuple[str, str]:
        return (cod_mp.strip(), bodega.strip())

    consumo: dict[tuple[str, str], float] = defaultdict(float)
    nombres_mp: dict[tuple[str, str], str] = {}
    unidades: dict[tuple[str, str], str] = {}
    costos: dict[tuple[str, str], float] = {}
    errores: list[str] = []
    resueltos: list[str] = []

    for cod_sm, detalle, qty in LINEAS_PREFACTURA:
        m = resolver_match(cod_sm, detalle, lookup)
        if m.get("estado_match") != "PROCESADO" or not m.get("cod_receta"):
            errores.append(f"Sin match: cod={cod_sm} detalle={detalle!r}")
            continue
        cod_receta = m["cod_receta"]
        variedad = m.get("variedad_matched") or ""
        resueltos.append(f"{m.get('nombre_producto')} {variedad} x{qty:g} → rec {cod_receta}")

        raw = get_ingredientes(cod_receta, variedad)
        lineas_mp, lineas_sub = preparar_ingredientes_descargo(
            raw, incluir_sub=incluir_sub
        )

        for ing in lineas_mp:
            cod_mp = (ing.get("cod_mp_sistema") or "").strip()
            if not cod_mp or cod_mp.startswith("#"):
                continue
            c = calcular_consumo(ing, qty)
            if c <= 0:
                continue
            mp_fb = None
            for bod in ("BOD-001", "BOD-002"):
                mp_fb = mp_sistema.get(_mp_key(cod_mp, bod))
                if mp_fb:
                    break
            bodega, err = resolver_bodega_receta(ing, mp_fb)
            if err or not bodega:
                errores.append(f"MP {cod_mp} sin bodega ({detalle})")
                continue
            k = _mp_key(cod_mp, bodega)
            consumo[k] += c
            mp_info = mp_sistema.get(k, mp_fb or {})
            nombres_mp[k] = ing.get("nombre_mp") or mp_info.get("nombre_mp") or ""
            unidades[k] = mp_info.get("unidad_base") or ing.get("unidad_base") or ""
            costos[k] = _sheet_float(mp_info.get("costo_unitario_ref") or 0)

        for ing in lineas_sub:
            mov, delta, warn = procesar_linea_sub_venta(
                ing,
                cantidad_vendida=qty,
                cod_receta=cod_receta,
                variedad=variedad,
                cod_venta=REF,
                fecha_v=FECHA,
                hora_raw=HORA,
                mp_sistema=mp_sistema,
                subs_meta=subs_meta,
                mp_key_fn=_mp_key,
                iso_fecha_hora_mov=_iso_fecha_hora_mov,
            )
            if warn:
                errores.append(f"SUB {ing.get('cod_subreceta')} ({detalle}): {warn}")
            elif delta:
                k = delta[0], delta[1]
                consumo[k] += delta[2]
                mp_info = mp_sistema.get(k, {})
                nombres_mp[k] = mp_info.get("nombre_mp") or ""
                unidades[k] = mp_info.get("unidad_base") or ""
                costos[k] = _sheet_float(mp_info.get("costo_unitario_ref") or 0)

    movs: list[dict] = []
    for (cod_mp, bodega), cant in sorted(consumo.items(), key=lambda x: (-x[1], x[0])):
        if cant <= 0:
            continue
        cu = costos.get((cod_mp, bodega), 0.0)
        movs.append(
            {
                "cod_mov": f"MOV-{FECHA.replace('-', '')}-{cod_mp}-{uuid.uuid4().hex[:12]}",
                "fecha": _iso_fecha_hora_mov(FECHA, HORA),
                "tipo_mov": "SALIDA_VENTA",
                "cod_mp_sistema": cod_mp,
                "nombre_mp": nombres_mp.get((cod_mp, bodega), ""),
                "cod_bodega_origen": bodega,
                "cod_bodega_destino": None,
                "cantidad_mov": round(cant, 4),
                "unidad_base": unidades.get((cod_mp, bodega), ""),
                "costo_unitario": cu,
                "costo_total": round(cant * cu, 4),
                "origen_documento": "AJUSTE_MANUAL",
                "num_documento": REF,
                "registrado_por": "AGENTE",
                "observaciones": (
                    f"Descargo manual prefactura PENDIENTES #3 / SmartMenu doc {DOC_SMART} "
                    f"({REF})"
                ),
            }
        )

    return movs, resueltos + errores


def main() -> int:
    from supabase import create_client

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--produccion", action="store_true", help="Inserta movimientos")
    p.add_argument(
        "--recalcular-stock",
        action="store_true",
        help="Tras insertar, ejecuta recalcular_stock_sheets.py --produccion",
    )
    args = p.parse_args()

    sb = create_client(
        __import__("os").environ["SUPABASE_URL"],
        __import__("os").environ["SUPABASE_KEY"],
    )

    if _ya_descargado(sb):
        print(f"Ya existen movimientos con num_documento={REF}. Nada que hacer.")
        return 0

    movs, log = _construir_movimientos()
    print(f"\n=== Descargo prefactura {REF} ===")
    for line in log:
        prefix = "  WARN" if line.startswith("Sin") or "sin bodega" in line or line.startswith("SUB") else "  OK"
        print(f"{prefix}: {line}" if prefix != "  OK" else f"  · {line}")

    print(f"\nMovimientos a insertar: {len(movs)}")
    total_cant = sum(m["cantidad_mov"] for m in movs)
    print(f"Cantidad total (mixta unidades): {total_cant:.2f}")
    print("\nTop consumo:")
    for m in sorted(movs, key=lambda x: -x["cantidad_mov"])[:15]:
        print(
            f"  MP {m['cod_mp_sistema']} @ {m['cod_bodega_origen']}: "
            f"{m['cantidad_mov']:.1f} {m['unidad_base']}"
        )

    if not args.produccion:
        print("\n[DRY RUN] Agrega --produccion para escribir mov_inventario.")
        return 0

    if not movs:
        print("ERROR: sin movimientos — revisar matches arriba.")
        return 1

    sb.table("mov_inventario").insert(movs).execute()
    print(f"\nOK — {len(movs)} movimientos insertados ({REF})")

    if args.recalcular_stock:
        r = subprocess.run(
            [sys.executable, str(ROOT / "recalcular_stock_sheets.py"), "--produccion"],
            cwd=str(ROOT),
        )
        return r.returncode

    # Actualización rápida en Sheets (mismo patrón descargo_inventario)
    from descargo_inventario import actualizar_stocks_sheets_batch, cargar_mp_sistema

    mp_sistema = cargar_mp_sistema()
    batch: dict[tuple[str, str], float] = {}
    for m in movs:
        k = (m["cod_mp_sistema"], m["cod_bodega_origen"])
        if k in mp_sistema:
            from descargo_inventario import _sheet_float

            prev = _sheet_float(mp_sistema[k].get("stock_actual") or 0)
            batch[k] = prev - m["cantidad_mov"]
    if batch:
        actualizar_stocks_sheets_batch(batch)
        print(f"Stock Sheets actualizado ({len(batch)} filas MP×bodega).")
        print("Recomendado: python recalcular_stock_sheets.py --produccion")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

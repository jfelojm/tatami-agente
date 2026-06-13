"""Inserta SALIDA_VENTA SUB pendientes (batches barra) sin tocar MPs ya descargados."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent

from descargo_inventario import (  # noqa: E402
    _iso_fecha_hora_mov,
    _mp_key,
    _resolver_cod_receta,
    actualizar_stocks_sheets_batch,
    cargar_mp_sistema,
    cargar_recetas,
    get_ingredientes,
)
from descargo_subreceta import (  # noqa: E402
    cargar_metadata_subrecetas,
    norm_cod_sub,
    preparar_ingredientes_descargo,
    procesar_linea_sub_venta,
    pseudo_mp_cod,
)
from reporte_semanal import conectar_supabase  # noqa: E402
from subrecetas_bodegas_stock import SUBRECETAS_BARRA  # noqa: E402

DESDE_DEFAULT = "2026-06-03"


def _ventas_con_batch_pendiente(sb, desde: str, hasta: str | None) -> list[dict]:
    offset = 0
    ventas: list[dict] = []
    while True:
        q = (
            sb.table("hist_ventas")
            .select("*")
            .eq("estado_match", "PROCESADO")
            .gte("fecha", desde)
        )
        if hasta:
            q = q.lte("fecha", hasta)
        chunk = q.range(offset, offset + 999).execute().data or []
        ventas.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000

    pendientes: list[dict] = []
    for v in ventas:
        cod_receta = _resolver_cod_receta(v) or ""
        var = v.get("variedad_smart_menu")
        ings = get_ingredientes(cod_receta, var)
        _, lineas_sub = preparar_ingredientes_descargo(ings, incluir_sub=True)
        subs_batch = [
            x
            for x in lineas_sub
            if norm_cod_sub(x.get("cod_subreceta") or "") in SUBRECETAS_BARRA
        ]
        if not subs_batch:
            continue

        cod_venta = v["cod_venta"]
        movs = (
            sb.table("mov_inventario")
            .select("cod_mp_sistema")
            .eq("num_documento", cod_venta)
            .eq("tipo_mov", "SALIDA_VENTA")
            .execute()
            .data
            or []
        )
        mov_subs = {
            m["cod_mp_sistema"]
            for m in movs
            if str(m.get("cod_mp_sistema", "")).startswith("SUB-")
        }
        esperados = {pseudo_mp_cod(norm_cod_sub(x["cod_subreceta"])) for x in subs_batch}
        if esperados - mov_subs:
            pendientes.append(v)
    return pendientes


def descargar_sub_pendiente(*, desde: str, hasta: str | None, dry_run: bool, recalcular: bool) -> int:
    sb = conectar_supabase()
    cargar_recetas()
    mp_sistema = cargar_mp_sistema()
    subs_meta = cargar_metadata_subrecetas()

    pendientes = _ventas_con_batch_pendiente(sb, desde, hasta)
    print(f"Ventas con batch SUB pendiente: {len(pendientes)}")

    if not pendientes:
        print("Nada pendiente.")
        return 0

    insertados = 0
    stocks_actualizados: set[tuple[str, str]] = set()

    for v in pendientes:
        cod_venta = v["cod_venta"]
        cod_receta = _resolver_cod_receta(v) or ""
        var = v.get("variedad_smart_menu")
        qty = float(v.get("cantidad_vendida") or 1)
        ings = get_ingredientes(cod_receta, var)
        _, lineas_sub = preparar_ingredientes_descargo(ings, incluir_sub=True)
        lineas_batch = [
            x
            for x in lineas_sub
            if norm_cod_sub(x.get("cod_subreceta") or "") in SUBRECETAS_BARRA
        ]

        movs_existentes = {
            m["cod_mp_sistema"]
            for m in (
                sb.table("mov_inventario")
                .select("cod_mp_sistema")
                .eq("num_documento", cod_venta)
                .eq("tipo_mov", "SALIDA_VENTA")
                .execute()
                .data
                or []
            )
            if str(m.get("cod_mp_sistema", "")).startswith("SUB-")
        }

        movs_nuevos: list[dict] = []
        deltas: list[tuple[str, str, float]] = []

        for ing in lineas_batch:
            cod_sub = norm_cod_sub(ing.get("cod_subreceta") or "")
            cod_mp = pseudo_mp_cod(cod_sub)
            if cod_mp in movs_existentes:
                continue

            mov, delta, warn = procesar_linea_sub_venta(
                ing,
                cantidad_vendida=qty,
                cod_receta=cod_receta,
                variedad=var,
                cod_venta=cod_venta,
                fecha_v=v.get("fecha"),
                hora_raw=v.get("hora"),
                mp_sistema=mp_sistema,
                subs_meta=subs_meta,
                mp_key_fn=_mp_key,
                iso_fecha_hora_mov=_iso_fecha_hora_mov,
            )
            if warn:
                print(f"  WARN {cod_venta} {cod_sub}: {warn}")
                continue
            if mov and delta:
                movs_nuevos.append(mov)
                deltas.append(delta)
                print(
                    f"  {v.get('fecha')} {cod_venta} | {v.get('nombre_producto')} x{qty} | "
                    f"{cod_sub} -{mov['cantidad_mov']} {mov.get('unidad_base')} @ {mov['cod_bodega_origen']}"
                )

        if not movs_nuevos:
            continue

        if dry_run:
            insertados += len(movs_nuevos)
            continue

        sb.table("mov_inventario").insert(movs_nuevos).execute()
        insertados += len(movs_nuevos)

        for cod_mp, bodega, consumo in deltas:
            k = _mp_key(cod_mp, bodega)
            if k in mp_sistema:
                from descargo_inventario import _sheet_float

                stock = _sheet_float(mp_sistema[k].get("stock_actual") or 0)
                mp_sistema[k]["stock_actual"] = stock - consumo
                stocks_actualizados.add(k)

    print(f"\nMovimientos SUB {'a insertar' if dry_run else 'insertados'}: {insertados}")

    if dry_run:
        print("Ejecuta con --produccion para aplicar.")
        return 0

    if stocks_actualizados:
        batch = {
            k: float(mp_sistema[k].get("stock_actual") or 0)
            for k in stocks_actualizados
        }
        print(f"Actualizando {len(batch)} filas SUB en Sheets…")
        actualizar_stocks_sheets_batch(batch)

    if recalcular:
        print("\nRecalculando stock SUB batches…")
        for sub in ("SUB-051", "SUB-052", "SUB-053", "SUB-054"):
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "recalcular_stock_sheets.py"),
                    "--produccion",
                    "--cod-mp",
                    sub,
                ],
                cwd=str(ROOT),
                check=False,
            )

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desde", default=DESDE_DEFAULT)
    p.add_argument("--hasta", default=None)
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--recalcular-stock", action="store_true")
    args = p.parse_args()
    return descargar_sub_pendiente(
        desde=args.desde.strip()[:10],
        hasta=(args.hasta.strip()[:10] if args.hasta else None),
        dry_run=not args.produccion,
        recalcular=args.recalcular_stock,
    )


if __name__ == "__main__":
    raise SystemExit(main())

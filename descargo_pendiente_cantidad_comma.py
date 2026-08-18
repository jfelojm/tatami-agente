"""
Backfill SALIDA_VENTA omitidas por cantidad con coma en BD_RECETAS_DETALLE.

Tras corregir calcular_consumo / calcular_consumo_sub, inserta movimientos faltantes
sin duplicar líneas ya registradas (p. ej. SUB-048 con cantidad entera).

Uso:
  python descargo_pendiente_cantidad_comma.py --dry-run
  python descargo_pendiente_cantidad_comma.py --produccion --sub SUB-061
  python descargo_pendiente_cantidad_comma.py --produccion --recalcular-stock
"""

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

from auditar_descargo_cantidad_comma import _cantidad_afectada  # noqa: E402
from descargo_inventario import (  # noqa: E402
    _fecha_min_descargo_ventas,
    _iso_fecha_hora_mov,
    _mp_key,
    _resolver_cod_receta,
    _sheet_float,
    actualizar_stocks_sheets_batch,
    calcular_consumo,
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
from bodegas_config import resolver_bodega_receta  # noqa: E402
from reporte_semanal import conectar_supabase  # noqa: E402
from recetas_detalle import es_linea_subreceta  # noqa: E402


def _cod_linea_descargo(ing: dict) -> str:
    if es_linea_subreceta(ing):
        return pseudo_mp_cod(norm_cod_sub(ing.get("cod_subreceta") or ""))
    return (ing.get("cod_mp_sistema") or "").strip()


def _linea_afectada_comma(ing: dict) -> bool:
    return _cantidad_afectada(str(ing.get("cantidad") or ""))


def _pasa_filtro_item(ing: dict, filtro_sub: str | None, filtro_mp: str | None) -> bool:
    if not filtro_sub and not filtro_mp:
        return True
    if filtro_sub and es_linea_subreceta(ing):
        return norm_cod_sub(ing.get("cod_subreceta") or "") == norm_cod_sub(filtro_sub)
    if filtro_mp and not es_linea_subreceta(ing):
        return (ing.get("cod_mp_sistema") or "").strip().lstrip("0") == filtro_mp.lstrip("0")
    return False


def _linea_pendiente(
    ing: dict, filtro_sub: str | None, filtro_mp: str | None
) -> bool:
    return _linea_afectada_comma(ing) and _pasa_filtro_item(ing, filtro_sub, filtro_mp)


def _ventas_a_revisar(sb, desde: str, hasta: str | None) -> list[dict]:
    offset = 0
    out: list[dict] = []
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
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return out


def _mov_mp(
    ing: dict,
    *,
    cantidad_v: float,
    cod_receta: str,
    variedad,
    cod_venta: str,
    fecha_v,
    hora_raw,
    mp_sistema: dict,
) -> tuple[dict | None, tuple[str, str, float] | None, str | None]:
    cod_mp = (ing.get("cod_mp_sistema") or "").strip()
    if not cod_mp or cod_mp.startswith("#"):
        return None, None, "cod_mp vacío"

    consumo = calcular_consumo(ing, cantidad_v)
    if consumo <= 0:
        return None, None, None

    mp_fb = None
    for bod_try in ("BOD-001", "BOD-002"):
        mp_fb = mp_sistema.get(_mp_key(cod_mp, bod_try))
        if mp_fb:
            break

    bodega, err_bod = resolver_bodega_receta(ing, mp_fb)
    if err_bod or not bodega:
        return None, None, f"MP {cod_mp}: {err_bod or 'sin bodega'}"

    mp_info = mp_sistema.get(_mp_key(cod_mp, bodega), mp_fb or {})
    unidad = mp_info.get("unidad_base", "") or ing.get("unidad_base", "")
    costo_u = _sheet_float(mp_info.get("costo_unitario_ref", 0) or 0)
    cod_mov = (
        f"MOV-{str(fecha_v or '').replace('-', '')[:8]}-{cod_mp}-"
        f"{uuid.uuid4().hex[:16]}"
    )
    mov = {
        "cod_mov": cod_mov,
        "fecha": _iso_fecha_hora_mov(fecha_v, hora_raw),
        "tipo_mov": "SALIDA_VENTA",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": ing.get("nombre_mp", "") or mp_info.get("nombre_mp", ""),
        "cod_bodega_origen": bodega,
        "cod_bodega_destino": None,
        "cantidad_mov": round(consumo, 4),
        "unidad_base": unidad,
        "costo_unitario": costo_u,
        "costo_total": round(consumo * costo_u, 4),
        "origen_documento": "VENTA_SMART_MENU",
        "num_documento": cod_venta,
        "registrado_por": "AGENTE",
        "observaciones": (
            f"Backfill comma-fix receta {cod_receta} var={variedad} bod={bodega}"
        ),
    }
    return mov, (cod_mp, bodega, consumo), None


def descargar_pendiente(
    *,
    desde: str,
    hasta: str | None,
    dry_run: bool,
    recalcular: bool,
    filtro_sub: str | None,
    filtro_mp: str | None,
) -> int:
    sb = conectar_supabase()
    cargar_recetas()
    mp_sistema = cargar_mp_sistema()
    subs_meta = cargar_metadata_subrecetas()

    ventas = _ventas_a_revisar(sb, desde, hasta)
    print(f"Ventas a revisar: {len(ventas)} (desde {desde})")

    insertados = 0
    ventas_tocadas = 0
    stocks_actualizados: set[tuple[str, str]] = set()
    codigos_insertados: set[str] = set()

    for v in ventas:
        cod_venta = v.get("cod_venta")
        if not cod_venta:
            continue
        cod_receta = _resolver_cod_receta(v) or ""
        var = v.get("variedad_smart_menu")
        qty = float(v.get("cantidad_vendida") or 1)
        ings = get_ingredientes(cod_receta, var)
        afectadas = [i for i in ings if _linea_pendiente(i, filtro_sub, filtro_mp)]
        if not afectadas:
            continue

        movs_exist = {
            (m.get("cod_mp_sistema") or "").strip()
            for m in (
                sb.table("mov_inventario")
                .select("cod_mp_sistema")
                .eq("num_documento", cod_venta)
                .eq("tipo_mov", "SALIDA_VENTA")
                .execute()
                .data
                or []
            )
        }

        movs_nuevos: list[dict] = []
        deltas: list[tuple[str, str, float]] = []

        lineas_mp, lineas_sub = preparar_ingredientes_descargo(ings, incluir_sub=True)
        for ing in lineas_mp:
            if not _linea_pendiente(ing, filtro_sub, filtro_mp):
                continue
            cod = _cod_linea_descargo(ing)
            if cod in movs_exist:
                continue
            mov, delta, warn = _mov_mp(
                ing,
                cantidad_v=qty,
                cod_receta=cod_receta,
                variedad=var,
                cod_venta=cod_venta,
                fecha_v=v.get("fecha"),
                hora_raw=v.get("hora"),
                mp_sistema=mp_sistema,
            )
            if warn:
                print(f"  WARN {cod_venta} {cod}: {warn}")
                continue
            if mov and delta:
                movs_nuevos.append(mov)
                deltas.append(delta)

        for ing in lineas_sub:
            if not _linea_pendiente(ing, filtro_sub, filtro_mp):
                continue
            cod = _cod_linea_descargo(ing)
            if cod in movs_exist:
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
                print(f"  WARN {cod_venta} {cod}: {warn}")
                continue
            if mov and delta:
                mov["observaciones"] = (
                    (mov.get("observaciones") or "")
                    + " | backfill comma-fix"
                ).strip(" |")
                movs_nuevos.append(mov)
                deltas.append(delta)

        if not movs_nuevos:
            continue

        ventas_tocadas += 1
        for mov in movs_nuevos:
            print(
                f"  {v.get('fecha')} {cod_venta} | {v.get('nombre_producto')} x{qty} | "
                f"{mov['cod_mp_sistema']} -{mov['cantidad_mov']} @ {mov['cod_bodega_origen']}"
            )
            codigos_insertados.add(str(mov["cod_mp_sistema"]))

        if dry_run:
            insertados += len(movs_nuevos)
            continue

        sb.table("mov_inventario").insert(movs_nuevos).execute()
        insertados += len(movs_nuevos)

        for cod_mp, bodega, consumo in deltas:
            k = _mp_key(cod_mp, bodega)
            if k in mp_sistema:
                stock = _sheet_float(mp_sistema[k].get("stock_actual") or 0)
                mp_sistema[k]["stock_actual"] = stock - consumo
                stocks_actualizados.add(k)

    print(
        f"\nMovimientos {'a insertar' if dry_run else 'insertados'}: {insertados} "
        f"en {ventas_tocadas} ventas"
    )
    if codigos_insertados:
        print("Códigos:", ", ".join(sorted(codigos_insertados)))

    if dry_run:
        print("Ejecuta con --produccion para aplicar.")
        return 0

    if stocks_actualizados:
        batch = {
            k: float(mp_sistema[k].get("stock_actual") or 0) for k in stocks_actualizados
        }
        print(f"Actualizando {len(batch)} filas en Sheets…")
        actualizar_stocks_sheets_batch(batch)

    if recalcular and codigos_insertados:
        print("\nRecalculando stock desde mov_inventario…")
        for cod in sorted(codigos_insertados):
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "recalcular_stock_sheets.py"),
                    "--produccion",
                    "--cod-mp",
                    cod.replace("SUB-", "") if cod.startswith("SUB-") else cod,
                ],
                cwd=str(ROOT),
                check=False,
            )
            # SUB pseudo-MP: recalcular con cod completo
            if cod.startswith("SUB-"):
                subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "recalcular_stock_sheets.py"),
                        "--produccion",
                        "--cod-mp",
                        cod,
                    ],
                    cwd=str(ROOT),
                    check=False,
                )

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desde", default=None, help="Default: TATAMI_FECHA_MIN_DESCARGO_VENTAS")
    p.add_argument("--hasta", default=None)
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--recalcular-stock", action="store_true")
    p.add_argument("--sub", default=None, help="Solo esta subreceta (ej. SUB-061)")
    p.add_argument("--mp", default=None, help="Solo este MP")
    args = p.parse_args()
    desde = (args.desde or _fecha_min_descargo_ventas()).strip()[:10]
    return descargar_pendiente(
        desde=desde,
        hasta=(args.hasta.strip()[:10] if args.hasta else None),
        dry_run=not args.produccion,
        recalcular=args.recalcular_stock,
        filtro_sub=args.sub,
        filtro_mp=args.mp,
    )


if __name__ == "__main__":
    raise SystemExit(main())

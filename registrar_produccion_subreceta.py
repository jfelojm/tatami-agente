"""
Registra producción de subreceta: baja MPs del detalle y entra stock del semi (pseudo-MP SUB-xxx).

Compatible con recalcular_stock_sheets:
  - MPs: AJUSTE_NEGATIVO (origen = bodega línea)
  - Semi: ENTRADA (destino = bodega producción)

Uso:
  python registrar_produccion_subreceta.py --cod 051 --dry-run
  python registrar_produccion_subreceta.py --cod 051 052 053 054 --bodega BOD-002 --por Eduardo --produccion
  python registrar_produccion_subreceta.py --cod 051 --cantidad 2250 --produccion --recalcular
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

from bodegas_config import normalizar_cod_bodega
from calcular_costo_subrecetas import _costo_mp, _safe_float, cargar_costos_mp
from descargo_subreceta import pseudo_mp_cod, cargar_metadata_subrecetas
from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas,
    cargar_bd_subrecetas_detalle,
    es_linea_mp_detalle,
    es_linea_subreceta_hijo,
)
from google_credentials import google_credentials

load_dotenv(override=False)


def _abrir_maestro():
    from google_credentials import open_gspread_workbook

    return open_gspread_workbook()


def _norm_sub(cod: str) -> str:
    from codigos_subreceta import cod_sub_canonico

    return cod_sub_canonico(cod)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _subs_meta_desde_cab(cab: dict[str, dict]) -> dict[str, dict]:
    from descargo_subreceta import norm_cod_sub, pseudo_mp_cod

    activos = ("SI", "S", "YES", "1", "TRUE")
    out: dict[str, dict] = {}
    for cod, info in cab.items():
        act = (info.get("activa") or "SI").strip().upper()
        if act not in activos:
            continue
        nk = norm_cod_sub(cod)
        if not nk:
            continue
        out[nk] = {
            **info,
            "cod_subreceta": nk,
            "cod_mp_pseudo": pseudo_mp_cod(nk),
            "nombre_subreceta": (info.get("nombre_subreceta") or "").strip(),
            "unidad": (info.get("unidad") or "gr").strip(),
            "costo_unitario_estandar": _safe_float(info.get("costo_unitario_estandar"), 0.0),
        }
    return out


def _cargar_mapa_stock(sh) -> dict[tuple[str, str], float]:
    """(cod_mp, cod_bodega) -> stock_actual."""
    from sheet_numbers import parse_sheet_number

    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = next(
        i for i, r in enumerate(vals) if any((c or "").strip() == "cod_mp_sistema" for c in r)
    )
    headers = [(c or "").strip() for c in vals[hi]]
    icod = headers.index("cod_mp_sistema")
    ibod = headers.index("cod_bodega")
    istk = headers.index("stock_actual")
    out: dict[tuple[str, str], float] = {}
    for row in vals[hi + 1 :]:
        if len(row) <= max(icod, ibod, istk):
            continue
        cod = row[icod].strip()
        bod = normalizar_cod_bodega(row[ibod])
        if cod and bod:
            out[(cod, bod)] = parse_sheet_number(row[istk], 0.0)
    return out


def _cargar_mapa_nombres_mp(sh) -> dict[str, str]:
    """cod_mp_sistema -> nombre_mp (primera fila no vacía en BD_MP_SISTEMA)."""
    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = next(
        i for i, r in enumerate(vals) if any((c or "").strip() == "cod_mp_sistema" for c in r)
    )
    headers = [(c or "").strip() for c in vals[hi]]
    icod = headers.index("cod_mp_sistema")
    inom = headers.index("nombre_mp")
    out: dict[str, str] = {}
    for row in vals[hi + 1 :]:
        if len(row) <= max(icod, inom):
            continue
        cod = row[icod].strip()
        nom = row[inom].strip()
        if cod and nom and cod not in out:
            out[cod] = nom
    return out


def _resolver_nombre_mp(
    cod_mp: str,
    *,
    nombre_detalle: str = "",
    nombres_mp: dict[str, str] | None = None,
    subs_meta: dict[str, dict] | None = None,
) -> str:
    nom = (nombre_detalle or "").strip()
    if nom:
        return nom
    if nombres_mp:
        nom = (nombres_mp.get(cod_mp) or "").strip()
        if nom:
            return nom
    if subs_meta and cod_mp:
        from codigos_subreceta import cod_sub_canonico

        nk = cod_sub_canonico(cod_mp.replace("SUB-", "") if not cod_mp.startswith("SUB-") else cod_mp)
        meta = subs_meta.get(nk) or {}
        nom = (meta.get("nombre_subreceta") or "").strip()
        if nom:
            return nom
    return cod_mp


def planificar_produccion(
    cod_sub: str,
    *,
    cantidad_producida: float | None,
    bodega_destino: str,
    sh=None,
    cab: dict | None = None,
    por_padre: dict | None = None,
    costos_mp: dict | None = None,
    subs_meta: dict | None = None,
    stock_map: dict[tuple[str, str], float] | None = None,
) -> dict:
    cod = _norm_sub(cod_sub)
    sh = sh or _abrir_maestro()
    cab = cab if cab is not None else cargar_bd_subrecetas(sh)
    if cod not in cab:
        raise ValueError(f"Subreceta {cod} no existe en BD_SUBRECETAS")

    info = cab[cod]
    rend = _safe_float(info.get("rendimiento_estandar"))
    if rend <= 0:
        raise ValueError(f"Subreceta {cod}: rendimiento_estandar inválido")

    cant_prod = cantidad_producida if cantidad_producida and cantidad_producida > 0 else rend
    factor = cant_prod / rend
    unidad = (info.get("unidad") or "ml").strip()

    por_padre = por_padre if por_padre is not None else agrupar_detalle_por_padre(
        cargar_bd_subrecetas_detalle(sh)
    )
    lineas = por_padre.get(cod, [])
    if not lineas:
        raise ValueError(f"Subreceta {cod}: sin líneas en BD_SUBRECETAS_DETALLE")

    costos_mp = costos_mp if costos_mp is not None else cargar_costos_mp(sh)
    if subs_meta is None:
        subs_meta = _subs_meta_desde_cab(cab)
    nombres_mp = _cargar_mapa_nombres_mp(sh)
    meta = subs_meta.get(cod, {})
    costo_u_sub = _safe_float(meta.get("costo_unitario_estandar"))
    if costo_u_sub <= 0:
        costo_u_sub = _safe_float(info.get("costo_unitario_estandar"))

    salidas_mp: list[dict] = []
    avisos: list[str] = []

    for ln in lineas:
        if es_linea_subreceta_hijo(ln):
            avisos.append(
                f"Sub {cod} tiene hijo {ln.get('cod_subreceta_hijo')}: "
                "producir hijo antes o usar lote estándar completo"
            )
            continue
        if not es_linea_mp_detalle(ln):
            continue

        cod_mp = (ln.get("cod_mp_sistema") or "").strip()
        bod_linea = normalizar_cod_bodega(ln.get("cod_bodega"))
        bod = bod_linea or normalizar_cod_bodega(bodega_destino)
        cant_std = _safe_float(ln.get("cantidad"))
        merma = _safe_float(ln.get("merma_pct"))
        consumo = round(cant_std * factor * (1 + merma), 4)
        if consumo <= 0:
            continue

        cu, nota = _costo_mp(cod_mp, bod, costos_mp)
        if cu <= 0:
            avisos.append(f"MP {cod_mp}@{bod} sin costo ({nota})")

        stk = (stock_map or {}).get((cod_mp, bod))
        nombre_mp = _resolver_nombre_mp(
            cod_mp,
            nombre_detalle=(ln.get("nombre_mp") or ""),
            nombres_mp=nombres_mp,
            subs_meta=subs_meta,
        )
        if stk is not None and stk < consumo:
            avisos.append(
                f"{nombre_mp} ({cod_mp})@{bod}: stock {stk} < consumo {consumo} "
                f"(lote factor {factor:.4f})"
            )

        salidas_mp.append(
            {
                "cod_mp_sistema": cod_mp,
                "nombre_mp": nombre_mp,
                "cod_bodega": bod,
                "cantidad_mov": consumo,
                "unidad_base": (ln.get("unidad_base") or "gr").strip(),
                "costo_unitario": round(cu, 6),
                "cantidad_std": cant_std,
                "merma_pct": merma,
            }
        )

    cod_pseudo = pseudo_mp_cod(cod)
    entrada_sub = {
        "cod_mp_sistema": cod_pseudo,
        "nombre_mp": meta.get("nombre_subreceta") or info.get("nombre_subreceta") or cod,
        "cod_bodega": normalizar_cod_bodega(bodega_destino),
        "cantidad_mov": round(cant_prod, 4),
        "unidad_base": unidad,
        "costo_unitario": round(costo_u_sub, 6),
    }

    return {
        "cod_subreceta": cod,
        "nombre_subreceta": entrada_sub["nombre_mp"],
        "rendimiento_estandar": rend,
        "cantidad_producida": cant_prod,
        "factor": round(factor, 6),
        "unidad": unidad,
        "bodega_destino": normalizar_cod_bodega(bodega_destino),
        "salidas_mp": salidas_mp,
        "entrada_sub": entrada_sub,
        "avisos": avisos,
        "costo_lote_mp": round(sum(s["cantidad_mov"] * s["costo_unitario"] for s in salidas_mp), 4),
    }


def _mov_mp_salida(
    item: dict,
    *,
    cod_sub: str,
    doc: str,
    fecha: str,
    registrado_por: str,
    factor: float,
) -> dict:
    cod_mp = item["cod_mp_sistema"]
    bod = item["cod_bodega"]
    cant = item["cantidad_mov"]
    costo_u = item["costo_unitario"]
    ts = uuid.uuid4().hex[:12]
    return {
        "cod_mov": f"MOV-PROD-{fecha[:10].replace('-', '')}-{cod_mp}-{ts}",
        "fecha": fecha,
        "tipo_mov": "AJUSTE_NEGATIVO",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": item.get("nombre_mp") or cod_mp,
        "cod_bodega_origen": bod,
        "cod_bodega_destino": None,
        "cantidad_mov": cant,
        "unidad_base": item.get("unidad_base") or "gr",
        "costo_unitario": costo_u,
        "costo_total": round(cant * costo_u, 4),
        "origen_documento": "PRODUCCION_SUBRECETA",
        "num_documento": doc,
        "registrado_por": registrado_por,
        "observaciones": (
            f"Producción SUB {cod_sub} | consumo MP lote×{factor:.4f} | "
            f"std={item.get('cantidad_std')} merma={item.get('merma_pct')}"
        ),
    }


def _mov_sub_entrada(
    entrada: dict,
    *,
    cod_sub: str,
    doc: str,
    fecha: str,
    registrado_por: str,
) -> dict:
    cod_mp = entrada["cod_mp_sistema"]
    bod = entrada["cod_bodega"]
    cant = entrada["cantidad_mov"]
    costo_u = entrada["costo_unitario"]
    ts = uuid.uuid4().hex[:12]
    return {
        "cod_mov": f"MOV-PROD-IN-{fecha[:10].replace('-', '')}-{cod_mp}-{ts}",
        "fecha": fecha,
        "tipo_mov": "ENTRADA",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": entrada.get("nombre_mp") or cod_sub,
        "cod_bodega_origen": None,
        "cod_bodega_destino": bod,
        "cantidad_mov": cant,
        "unidad_base": entrada.get("unidad_base") or "ml",
        "costo_unitario": costo_u,
        "costo_total": round(cant * costo_u, 4),
        "origen_documento": "PRODUCCION_SUBRECETA",
        "num_documento": doc,
        "registrado_por": registrado_por,
        "observaciones": (
            f"Producción SUB {cod_sub} | entrada semi {cant} {entrada.get('unidad_base')} @ {bod}"
        ),
    }


def registrar(
    plan: dict,
    *,
    produccion: bool,
    registrado_por: str = "AGENTE",
) -> dict:
    fecha = _iso_now()
    doc = f"PROD-SUB-{plan['cod_subreceta']}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
    movs = []
    for s in plan["salidas_mp"]:
        movs.append(
            _mov_mp_salida(
                s,
                cod_sub=plan["cod_subreceta"],
                doc=doc,
                fecha=fecha,
                registrado_por=registrado_por,
                factor=plan["factor"],
            )
        )
    movs.append(
        _mov_sub_entrada(
            plan["entrada_sub"],
            cod_sub=plan["cod_subreceta"],
            doc=doc,
            fecha=fecha,
            registrado_por=registrado_por,
        )
    )

    if not produccion:
        return {"dry_run": True, "documento": doc, "movimientos": movs, "plan": plan}

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    insertados = 0
    for m in movs:
        sb.table("mov_inventario").insert(m).execute()
        insertados += 1
    return {
        "dry_run": False,
        "documento": doc,
        "movimientos_insertados": insertados,
        "cod_subreceta": plan["cod_subreceta"],
    }


def _imprimir_plan(plan: dict) -> None:
    print(f"\n--- SUB {plan['cod_subreceta']} {plan['nombre_subreceta']} ---")
    print(
        f"  Lote: {plan['cantidad_producida']} {plan['unidad']} "
        f"(rend std {plan['rendimiento_estandar']}, factor {plan['factor']})"
    )
    print(f"  Bodega entrada semi: {plan['bodega_destino']}")
    print(f"  Costo MPs (teórico lote): ${plan['costo_lote_mp']:.2f}")
    print(f"  Entrada {plan['entrada_sub']['cod_mp_sistema']}: "
          f"{plan['entrada_sub']['cantidad_mov']} {plan['entrada_sub']['unidad_base']} "
          f"@ ${plan['entrada_sub']['costo_unitario']:.6f}/u")
    print("  Salidas MP:")
    for s in plan["salidas_mp"]:
        print(
            f"    {s['cod_mp_sistema']} @ {s['cod_bodega']}: "
            f"-{s['cantidad_mov']} {s['unidad_base']} (cu ${s['costo_unitario']:.6f})"
        )
    if plan["avisos"]:
        print("  AVISOS:")
        for a in plan["avisos"]:
            print(f"    ! {a}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cod", nargs="+", required=True, help="cod_subreceta ej. 051 052")
    p.add_argument(
        "--cantidad",
        type=float,
        default=None,
        help="Cantidad producida (default: rendimiento estándar del lote)",
    )
    p.add_argument("--bodega", default="BOD-002", help="Bodega donde entra el semi (default barra)")
    p.add_argument("--por", default="AGENTE", help="registrado_por")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument(
        "--recalcular",
        action="store_true",
        help="Tras insertar, ejecutar recalcular_stock_sheets.py --produccion",
    )
    p.add_argument("--force", action="store_true", help="Insertar aunque haya avisos de stock")
    args = p.parse_args()

    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        raise SystemExit(2)

    sh = _abrir_maestro()
    print("Cargando maestros (una sola lectura Sheets)...")
    cab = cargar_bd_subrecetas(sh)
    por_padre = agrupar_detalle_por_padre(cargar_bd_subrecetas_detalle(sh))
    costos_mp = cargar_costos_mp(sh)
    subs_meta = _subs_meta_desde_cab(cab)
    stock_map = _cargar_mapa_stock(sh)

    resultados = []
    for raw in args.cod:
        plan = planificar_produccion(
            raw,
            cantidad_producida=args.cantidad,
            bodega_destino=args.bodega,
            sh=sh,
            cab=cab,
            por_padre=por_padre,
            costos_mp=costos_mp,
            subs_meta=subs_meta,
            stock_map=stock_map,
        )
        _imprimir_plan(plan)
        if plan["avisos"] and not args.force and args.produccion:
            print(f"  OMITIDO {plan['cod_subreceta']}: hay avisos (use --force para forzar)")
            continue
        res = registrar(plan, produccion=args.produccion, registrado_por=args.por)
        resultados.append(res)
        if res.get("dry_run"):
            print(f"  [DRY-RUN] {len(res['movimientos'])} movimientos ({doc_preview(res)})")
        else:
            print(
                f"  OK insertados {res['movimientos_insertados']} mov "
                f"doc={res['documento']}"
            )

    if args.produccion and args.recalcular and resultados:
        print("\nRecalculando stock Sheets...")
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "recalcular_stock_sheets.py", "--produccion"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        if r.returncode != 0:
            raise SystemExit(r.returncode)


def doc_preview(res: dict) -> str:
    if res.get("documento"):
        return res["documento"]
    movs = res.get("movimientos") or []
    if movs:
        return movs[0].get("num_documento", "")
    return ""


if __name__ == "__main__":
    main()

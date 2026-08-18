"""Traslada a BOD-005 las ENTRADAs de factura del 25-jun que cayeron en BOD-001 por catálogo viejo."""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import timedelta

from dateutil import parser
from dotenv import load_dotenv

load_dotenv(override=True)

from inventario_traslado import costo_ref_desde_filas_maestro, registrar_traslado_mp
from whatsapp_webhook import conectar_supabase, leer_bd_mp_sistema

ORIGEN = "BOD-001"
DESTINO = "BOD-005"
PROV_EXCEPCION = frozenset({"161", "172"})
VENTANA_HORAS = 6


def _facturas_ultima_corrida(sb) -> list[dict]:
    fp = (
        sb.table("facturas_procesadas")
        .select("num_factura,fecha_proceso,ruc_proveedor")
        .order("fecha_proceso", desc=True)
        .limit(30)
        .execute()
    )
    rows = fp.data or []
    if not rows:
        return []
    t0 = parser.isoparse(rows[0]["fecha_proceso"])
    return [
        r
        for r in rows
        if abs((t0 - parser.isoparse(r["fecha_proceso"])).total_seconds())
        <= VENTANA_HORAS * 3600
    ]


def _ruc_a_cod_prov() -> dict[str, str]:
    from whatsapp_webhook import leer_bd_prov

    out: dict[str, str] = {}
    for p in leer_bd_prov():
        ruc = (p.get("ruc_proveedor") or "").strip()
        cod = (p.get("cod_proveedor") or "").strip()
        if ruc and cod:
            out[ruc] = cod
    return out


def _ya_trasladado(sb, num_factura: str, cod_mp: str) -> bool:
    res = (
        sb.table("mov_inventario")
        .select("cod_mov")
        .eq("tipo_mov", "TRASLADO_ENTRADA")
        .eq("cod_bodega_destino", DESTINO)
        .eq("cod_mp_sistema", cod_mp)
        .eq("registrado_por", "CORRECCION_CATALOGO_005")
        .ilike("observaciones", f"%{num_factura}%")
        .limit(1)
        .execute()
    )
    return bool(res.data)


def listar_pendientes(sb, *, dry_run: bool = True) -> list[dict]:
    facturas = _facturas_ultima_corrida(sb)
    ruc_map = _ruc_a_cod_prov()
    pendientes: list[dict] = []

    for fac in facturas:
        num = fac["num_factura"]
        ruc = (fac.get("ruc_proveedor") or "").strip()
        cod_prov = ruc_map.get(ruc, "")
        if cod_prov in PROV_EXCEPCION:
            continue

        res = (
            sb.table("mov_inventario")
            .select(
                "cod_mp_sistema,nombre_mp,cantidad_mov,unidad_base,"
                "costo_unitario,costo_total,num_documento"
            )
            .eq("origen_documento", "FACTURA")
            .eq("tipo_mov", "ENTRADA")
            .eq("cod_bodega_destino", ORIGEN)
            .eq("num_documento", num)
            .execute()
        )
        for m in res.data or []:
            cod_mp = (m.get("cod_mp_sistema") or "").strip()
            cant = float(m.get("cantidad_mov") or 0)
            if cant <= 0 or not cod_mp:
                continue
            if _ya_trasladado(sb, num, cod_mp):
                continue
            pendientes.append(
                {
                    "num_factura": num,
                    "cod_prov": cod_prov,
                    "cod_mp": cod_mp,
                    "nombre_mp": (m.get("nombre_mp") or cod_mp).strip(),
                    "cantidad": cant,
                    "unidad_base": (m.get("unidad_base") or "gr").strip(),
                    "costo_unitario": float(m.get("costo_unitario") or 0),
                }
            )
    return pendientes


def ejecutar(pendientes: list[dict], *, dry_run: bool) -> tuple[int, list[str]]:
    if dry_run:
        return 0, []

    sb = conectar_supabase()
    rows = leer_bd_mp_sistema(force_refresh=True)
    ok = 0
    errs: list[str] = []

    # Agrupar por MP+cantidad si misma línea en varias facturas (raro)
    for p in pendientes:
        cod_mp = p["cod_mp"]
        cant = p["cantidad"]
        try:
            costo = p["costo_unitario"]
            if costo <= 0:
                costo = costo_ref_desde_filas_maestro(rows, cod_mp, ORIGEN)
            if costo <= 0:
                costo = costo_ref_desde_filas_maestro(rows, cod_mp, DESTINO)

            obs = (
                f"Corrección catálogo: factura {p['num_factura']} "
                f"ingresó en {ORIGEN} (debe {DESTINO})"
            )
            res = registrar_traslado_mp(
                sb,
                cod_mp=cod_mp,
                bodega_origen=ORIGEN,
                bodega_destino=DESTINO,
                cantidad=cant,
                nombre_mp=p["nombre_mp"],
                unidad_base=p["unidad_base"],
                costo_unitario_ref=costo,
                registrado_por="CORRECCION_CATALOGO_005",
                recalcular_sheets=True,
            )
            # Actualizar observaciones con ref factura (registrar_traslado no acepta obs custom)
            for suf in ("-SAL", "-ENT"):
                sb.table("mov_inventario").update({"observaciones": obs}).eq(
                    "cod_mov", res["cod_mov"] + suf
                ).execute()
            print(
                f"OK  {p['num_factura']} | MP {cod_mp} | "
                f"{cant} {p['unidad_base']} | {res['cod_mov']}"
            )
            ok += 1
        except Exception as e:
            msg = f"ERR {p['num_factura']} MP{cod_mp}: {e}"
            print(msg)
            errs.append(msg)
    return ok, errs


def main() -> int:
    dry = "--produccion" not in sys.argv
    sb = conectar_supabase()
    pendientes = listar_pendientes(sb, dry_run=dry)

    print(f"Traslados {ORIGEN} -> {DESTINO} (corrida facturas 25-jun)")
    print(f"Lineas pendientes: {len(pendientes)}")
    by_fac: dict[str, list] = defaultdict(list)
    for p in pendientes:
        by_fac[p["num_factura"]].append(p)
    for num, lines in sorted(by_fac.items()):
        print(f"\n  Factura {num} ({len(lines)} items)")
        for ln in lines:
            print(
                f"    MP {ln['cod_mp']} {ln['nombre_mp'][:40]} | "
                f"+{ln['cantidad']} {ln['unidad_base']}"
            )

    if dry:
        print("\n[DRY RUN] Ejecutar con: python ejecutar_traslado_corrida_001_005.py --produccion")
        return 0

    ok, errs = ejecutar(pendientes, dry_run=False)
    print(f"\nResumen: {ok}/{len(pendientes)} OK")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())

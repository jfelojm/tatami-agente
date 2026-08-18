"""Alta catálogo Celeste Crianza Colemun (MP 292) + reproceso factura 019-001-000054369."""
from __future__ import annotations

import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

load_dotenv(override=True)

NUM = "019-001-000054369"
RUC = "0992613092001"
COD_XML = "1031481-4"
COD_CAT = "1031481"
COD_PROV = "123"
COD_MP = "292"
DESC = "VINO CELESTE CRIANZA"


def main() -> int:
    from staging_common import find_header_row, open_master
    from supabase import create_client

    dry = "--dry-run" in sys.argv
    sh = open_master()

    ws_prov = sh.worksheet("BD_ITEMS_PROV")
    vals = ws_prov.get_all_values()
    hi = find_header_row(vals, "cod_item_prov")
    assert hi is not None
    headers = [(c or "").strip() for c in vals[hi]]
    icod = headers.index("cod_item_prov")
    iprov = headers.index("cod_proveedor")
    exists = False
    for row in vals[hi + 1 :]:
        if (row[iprov] if iprov < len(row) else "").strip() != COD_PROV:
            continue
        cod = (row[icod] if icod < len(row) else "").strip()
        if cod in (COD_CAT, COD_XML):
            exists = True
            print(f"Ya existe en BD_ITEMS_PROV: {cod}")
            break

    if not exists:
        row_map = {
            "nombre_proveedor": "COLEMUN S.A.",
            "cod_proveedor": COD_PROV,
            "cod_item_prov": COD_CAT,
            "descripcion_proveedor": DESC,
            "nombre_mp": "Vino Blanco Verdejo",
            "cod_mp_sistema": COD_MP,
            "unidad_compra": "uni",
            "factor_conversion": "1",
            "unidad_base_sistema": "uni",
            "cod_bodega_destino": "BOD-002",
            "activo": "SI",
            "precio_ref": "24,36",
            "fecha_precio_ref": "2026-07-30",
        }
        nueva = [row_map.get(h, "") for h in headers]
        if dry:
            print("[DRY] insertaría BD_ITEMS_PROV:", dict(zip(headers, nueva)))
        else:
            start = len(vals) + 1
            ws_prov.update(
                range_name=f"A{start}",
                values=[nueva],
                value_input_option=ValueInputOption.user_entered,
            )
            print(f"OK BD_ITEMS_PROV fila {start}: {COD_PROV}/{COD_CAT} -> {COD_MP}")

    ws_p = sh.worksheet("BD_ITEMS_PENDIENTES")
    pvals = ws_p.get_all_values()
    phi = find_header_row(pvals, "cod_item_xml") or find_header_row(pvals, "clave_unica")
    assert phi is not None
    ph = [(c or "").strip() for c in pvals[phi]]
    i_xml = ph.index("cod_item_xml") if "cod_item_xml" in ph else None
    i_fac = ph.index("num_factura") if "num_factura" in ph else None
    i_est = ph.index("estado") if "estado" in ph else None
    i_mp = ph.index("cod_mp_asignado") if "cod_mp_asignado" in ph else None
    updates = []
    for i, row in enumerate(pvals[phi + 1 :], start=phi + 2):
        xml = (row[i_xml] if i_xml is not None and i_xml < len(row) else "").strip()
        fac = (row[i_fac] if i_fac is not None and i_fac < len(row) else "").strip()
        if fac != NUM or xml not in (COD_XML, COD_CAT):
            continue
        print(f"Pendiente fila {i}: {xml}")
        if i_mp is not None:
            updates.append({"range": rowcol_to_a1(i, i_mp + 1), "values": [[COD_MP]]})
        if i_est is not None:
            updates.append({"range": rowcol_to_a1(i, i_est + 1), "values": [["REGISTRADO"]]})
    if updates and not dry:
        ws_p.batch_update(updates, value_input_option=ValueInputOption.user_entered)
        print(f"OK pendientes actualizados: {len(updates)} celdas")
    elif updates:
        print(f"[DRY] actualizaría {len(updates)} celdas")

    if dry:
        return 0

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    sb.table("facturas_procesadas").update(
        {
            "estado": "PARCIAL",
            "meta": {
                "reproceso": "celeste_crianza_292",
                "at": datetime.now().isoformat(),
            },
        }
    ).eq("num_factura", NUM).eq("ruc_proveedor", RUC).execute()
    print("facturas_procesadas -> PARCIAL")

    sri = (
        sb.table("sri_comprobantes_recibidos")
        .select("clave_acceso,estado")
        .eq("num_factura", NUM)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not sri:
        print("ERROR: no hay fila SRI")
        return 1
    clave = sri[0]["clave_acceso"]
    sb.table("sri_comprobantes_recibidos").update(
        {"estado": "DESCARGADO", "fecha_proceso": None}
    ).eq("clave_acceso", clave).execute()
    print("sri -> DESCARGADO", clave[:20])

    import procesar_facturas_drive as pfd

    pfd._items_prov_cache = None  # type: ignore

    from procesar_facturas_sri import SriConfig, fase_proceso

    res = fase_proceso(SriConfig.from_env(), corrida="FIX_CELESTE_292", dry_run=False)
    print(
        "fase_proceso",
        {
            k: res.get(k)
            for k in ("completas", "parciales", "omitidas", "errores", "procesados")
            if isinstance(res, dict) and k in res
        }
        or type(res).__name__,
    )

    from recepcion_compras_barra import confirmar_factura_ok, listar_por_recibir

    pend = [
        p
        for p in listar_por_recibir(solo_pendientes=True)
        if (p.get("num_factura") or "").strip() == NUM
        and (p.get("cod_mp_sistema") or "").strip() == COD_MP
        and "1031481" in (p.get("cod_item_xml") or "")
    ]
    print("pendientes cola Codorniu/celeste:", len(pend), pend)
    if pend:
        claves = [
            (p.get("clave_linea") or "").strip()
            for p in pend
            if (p.get("clave_linea") or "").strip()
        ]
        ok = confirmar_factura_ok(
            NUM,
            usuario="fix_celeste_292",
            dry_run=False,
            claves_linea=claves or None,
        )
        print(
            "OK recepción",
            {
                k: ok.get(k)
                for k in (
                    "ok",
                    "entradas",
                    "errores",
                    "claves_ok",
                    "cantidades",
                    "quedan_por_recibir",
                )
            },
        )
    else:
        print("WARN: no hay línea POR_RECIBIR para 1031481 — ¿ya entró?")

    movs = (
        sb.table("mov_inventario")
        .select("cod_mov,fecha,tipo_mov,cod_mp_sistema,cantidad_mov,num_documento,observaciones")
        .eq("num_documento", NUM)
        .eq("cod_mp_sistema", COD_MP)
        .execute()
        .data
        or []
    )
    print("movs 292 en factura:")
    for m in movs:
        print(" ", m)

    from staging_common import open_master as om

    ws = om().worksheet("BD_MP_SISTEMA")
    v = ws.get_all_values()
    hrow = find_header_row(v, "cod_mp_sistema")
    hh = [(c or "").strip() for c in v[hrow]]
    ic = hh.index("cod_mp_sistema")
    ib = hh.index("cod_bodega")
    ist = hh.index("stock_actual")
    for row in v[hrow + 1 :]:
        if (row[ic] if ic < len(row) else "").strip() != COD_MP:
            continue
        if (row[ib] if ib < len(row) else "").strip() != "BOD-002":
            continue
        print("stock BD_MP_SISTEMA 292@BOD-002 =", row[ist] if ist < len(row) else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

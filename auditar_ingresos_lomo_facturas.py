"""Audita ENTRADAs de lomo fino 047/552 desde facturas procesadas."""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)

MP_LOMOS = {
    "047": "LOMO FINO DE RES ITALIANA (ItalDeli)",
    "552": "LOMO FINO DE RES PIGGIS (Pacheco)",
}

# RUC conocidos validos (desde BD_PROV / historial)
RUC_VALIDOS = {
    "047": set(),  # se llena desde catalogo
    "552": set(),
}

RUC_INVALIDOS_CONOCIDOS = {
    "0104680590001": "Lopez Castro",
    "0102101052001": "Castro Pesantez",
}


def _sb():
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _cargar_proveedores():
    from whatsapp_webhook import leer_bd_prov

    prov_by_cod: dict[str, dict] = {}
    prov_by_ruc: dict[str, dict] = {}
    for p in leer_bd_prov():
        cod = (p.get("cod_proveedor") or "").strip()
        ruc = (p.get("ruc_proveedor") or "").strip()
        if cod:
            prov_by_cod[cod] = p
        if ruc:
            prov_by_ruc[ruc] = p
    return prov_by_cod, prov_by_ruc


def _cargar_items_lomo():
    import gspread
    from google_credentials import google_credentials

    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_item_prov" in r)
    h = [(c or "").strip() for c in vals[hi]]
    ic = h.index("cod_item_prov")
    ip = h.index("cod_proveedor")
    imp = h.index("cod_mp_sistema")
    idesc = h.index("descripcion_proveedor") if "descripcion_proveedor" in h else None
    iact = h.index("activo") if "activo" in h else None

    items: dict[str, list[dict]] = defaultdict(list)
    for row in vals[hi + 1 :]:
        mp = (row[imp] if imp < len(row) else "").strip()
        if mp not in MP_LOMOS:
            continue
        items[mp].append(
            {
                "cod_item_prov": (row[ic] if ic < len(row) else "").strip(),
                "cod_proveedor": (row[ip] if ip < len(row) else "").strip(),
                "descripcion": (row[idesc] if idesc is not None and idesc < len(row) else "").strip(),
                "activo": (row[iact] if iact is not None and iact < len(row) else "").strip(),
            }
        )
    return items


def _cargar_facturas(sb) -> dict[str, dict]:
    res = sb.table("facturas_procesadas").select("*").execute()
    return {f["num_factura"]: f for f in (res.data or [])}


def _cargar_entradas(sb, cod_mp: str) -> list[dict]:
    res = (
        sb.table("mov_inventario")
        .select("*")
        .eq("tipo_mov", "ENTRADA")
        .eq("cod_mp_sistema", cod_mp)
        .order("fecha")
        .execute()
    )
    return res.data or []


def _fmt_fecha(v: str) -> str:
    return (v or "")[:10]


def main() -> int:
    sb = _sb()
    prov_by_cod, prov_by_ruc = _cargar_proveedores()
    items_lomo = _cargar_items_lomo()
    facturas = _cargar_facturas(sb)

    print("=" * 72)
    print("AUDITORIA INGRESOS LOMO FINO POR FACTURA")
    print("=" * 72)

    for mp, etiqueta in MP_LOMOS.items():
        print(f"\n## MP {mp} — {etiqueta}")
        print("-" * 72)

        items = items_lomo.get(mp, [])
        cods_prov = sorted({i["cod_proveedor"] for i in items if i["cod_proveedor"]})
        print(f"Catalogo BD_ITEMS_PROV: {len(items)} items | proveedores: {', '.join(cods_prov) or 'ninguno'}")
        for it in items:
            act = it.get("activo") or "?"
            print(
                f"  [{act}] prov={it['cod_proveedor']} item={it['cod_item_prov']} "
                f"| {it['descripcion'][:60]}"
            )

        entradas = _cargar_entradas(sb, mp)
        desde_factura = [
            e
            for e in entradas
            if (e.get("origen_documento") or "").upper() in ("FACTURA", "FACTURAS")
            or (e.get("registrado_por") or "").upper() == "AGENTE"
            or e.get("num_documento")
        ]

        total_gr = 0.0
        total_revertido = 0.0
        total_activo = 0.0
        por_proveedor: dict[str, float] = defaultdict(float)
        sospechosas: list[dict] = []

        print(f"\nENTRADAs totales: {len(entradas)} | con documento/factura: {len(desde_factura)}")
        print(f"{'fecha':<12} {'factura':<14} {'cant':>10} {'bod':<8} {'prov':<22} {'estado':<12} obs")
        print("-" * 72)

        for e in desde_factura:
            cant = float(e.get("cantidad_mov") or 0)
            total_gr += cant
            obs = (e.get("observaciones") or "")
            revertido = "REVERTIDO" in obs.upper()
            if revertido:
                total_revertido += cant
            else:
                total_activo += cant

            num = (e.get("num_documento") or "").strip()
            fac = facturas.get(num, {})
            ruc = (fac.get("ruc_proveedor") or "").strip()
            prov = prov_by_ruc.get(ruc, {})
            prov_nom = (prov.get("razon_social") or prov.get("nombre_proveedor") or ruc or "?")[:22]
            cod_prov = (prov.get("cod_proveedor") or "").strip()

            por_proveedor[prov_nom] += cant if not revertido else 0

            # reglas de validez
            alerta = ""
            if mp == "552" and ruc in RUC_INVALIDOS_CONOCIDOS:
                alerta = "PROV_INVALIDO"
            elif mp == "047" and cod_prov and cod_prov not in cods_prov and cods_prov:
                alerta = "PROV_NO_CATALOGO"
            elif mp == "552" and cod_prov and cod_prov not in cods_prov and cods_prov:
                alerta = "PROV_NO_CATALOGO"
            elif not ruc and num:
                alerta = "SIN_RUC"
            elif revertido:
                alerta = "REVERTIDO"

            estado = alerta or "OK"
            if alerta and alerta != "REVERTIDO":
                sospechosas.append({**e, "_ruc": ruc, "_prov": prov_nom, "_alerta": alerta})

            print(
                f"{_fmt_fecha(e.get('fecha','')):<12} {num:<14} {cant:>10.1f} "
                f"{(e.get('cod_bodega_destino') or ''):<8} {prov_nom:<22} {estado:<12} "
                f"{obs[:40]}"
            )

        print("-" * 72)
        print(f"Total ingresado: {total_gr:,.1f} g | activo: {total_activo:,.1f} g | revertido: {total_revertido:,.1f} g")
        print("Por proveedor (activo):")
        for prov, gr in sorted(por_proveedor.items(), key=lambda x: -x[1]):
            if gr > 0:
                print(f"  {prov}: {gr:,.1f} g")

        if sospechosas:
            print(f"\nALERTAS activas: {len(sospechosas)}")
            for s in sospechosas:
                print(
                    f"  {s['_alerta']} | {_fmt_fecha(s.get('fecha'))} | {s.get('num_documento')} | "
                    f"{float(s.get('cantidad_mov') or 0):,.1f} g | {s['_prov']} | {s.get('cod_mov')}"
                )
        else:
            print("\nSin alertas en entradas activas.")

    # cruce: entradas 552 que deberian ser 047 o viceversa
    print("\n" + "=" * 72)
    print("CRUCE MP INCORRECTO (descripcion en observaciones)")
    print("=" * 72)
    for mp in MP_LOMOS:
        entradas = _cargar_entradas(sb, mp)
        for e in entradas:
            obs = (e.get("observaciones") or "").upper()
            if "REVERTIDO" in obs:
                continue
            desc = obs + " " + (e.get("nombre_mp") or "").upper()
            if mp == "552" and ("ITALIANA" in desc or "ITALDELI" in desc):
                print(f"  MP552 con ref Italiana: {e.get('cod_mov')} {e.get('num_documento')} {e.get('cantidad_mov')}")
            if mp == "047" and ("PIGGIS" in desc or "PACHECO" in desc):
                print(f"  MP047 con ref Piggis: {e.get('cod_mov')} {e.get('num_documento')} {e.get('cantidad_mov')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

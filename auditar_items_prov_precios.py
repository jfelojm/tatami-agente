"""
Auditoría BD_ITEMS_PROV vs facturas_procesadas y movimientos.

Uso:
  python auditar_items_prov_precios.py
  python auditar_items_prov_precios.py -o reporte_items_prov.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import Counter, defaultdict
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)


def _f(v) -> float | None:
    s = (str(v or "")).strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_mp(c: str) -> str:
    s = (c or "").strip()
    if not s:
        return ""
    n = s.lstrip("0")
    return n if n else "0"


def cargar_items_prov(sh) -> tuple[list[str], list[dict]]:
    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = next(
        i
        for i, r in enumerate(vals)
        if any((c or "").strip() == "cod_item_prov" for c in r)
    )
    headers = [(c or "").strip() for c in vals[hi]]
    rows = []
    for row in vals[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        d = {
            h: (row[i] if i < len(row) else "").strip()
            for i, h in enumerate(headers)
        }
        rows.append(d)
    return headers, rows


def cargar_facturas_procesadas() -> list[dict]:
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    out: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("facturas_procesadas")
            .select(
                "num_factura,fecha_factura,ruc_proveedor,estado,"
                "items_procesados,items_sin_match,fecha_proceso"
            )
            .order("fecha_proceso", desc=True)
            .range(offset, offset + 999)
            .execute()
        )
        data = chunk.data or []
        out.extend(data)
        if len(data) < 1000:
            break
        offset += 1000
    return out


def cargar_entradas_por_mp() -> dict[str, list[dict]]:
    """Última ENTRADA con costo por cod_mp (normalizado)."""
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    ultimo: dict[str, tuple[str, str, float]] = {}
    offset = 0
    while True:
        chunk = (
            sb.table("mov_inventario")
            .select("cod_mp_sistema,fecha,costo_unitario,tipo_mov,num_documento")
            .eq("tipo_mov", "ENTRADA")
            .gt("costo_unitario", 0)
            .order("fecha", desc=True)
            .range(offset, offset + 999)
            .execute()
        )
        for m in chunk.data or []:
            cod = _norm_mp(m.get("cod_mp_sistema") or "")
            if not cod or cod in ultimo:
                continue
            cu = float(m.get("costo_unitario") or 0)
            ultimo[cod] = (
                (m.get("fecha") or "")[:10],
                (m.get("num_documento") or ""),
                cu,
            )
        if len(chunk.data or []) < 1000:
            break
        offset += 1000
        if offset > 50000:
            break
    return {k: {"fecha": v[0], "factura": v[1], "costo_u": v[2]} for k, v in ultimo.items()}


def clasificar_fila(d: dict) -> str:
    activo = (d.get("activo") or "").strip().upper()
    if activo == "NO":
        return "inactivo"
    mp = (d.get("cod_mp_sistema") or "").strip()
    pr = _f(d.get("precio_ref"))
    fac = _f(d.get("factor_conversion"))
    uc = (d.get("unidad_compra") or "").strip()
    if not mp:
        return "sin_mp"
    if not pr or pr <= 0:
        if not fac or fac <= 0 or not uc:
            return "sin_precio_y_sin_conversion"
        return "sin_precio_ref"
    if not fac or fac <= 0 or not uc:
        return "tiene_precio_sin_conversion"
    # precio parece USD/gr pero unidad compra kg
    if uc.lower() in ("kg", "kilo", "kilogramo") and fac >= 100 and pr > 1:
        return "precio_posible_error_x1000"
    return "ok"


def main() -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    p = argparse.ArgumentParser()
    p.add_argument("-o", "--output", default="")
    args = p.parse_args()

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    _, items = cargar_items_prov(sh)

    facturas = cargar_facturas_procesadas()
    entradas_mp = cargar_entradas_por_mp()

    activos = [d for d in items if (d.get("activo") or "").upper() != "NO"]
    con_mp = [d for d in activos if (d.get("cod_mp_sistema") or "").strip()]
    sin_precio = [
        d
        for d in con_mp
        if not _f(d.get("precio_ref")) or _f(d.get("precio_ref")) <= 0
    ]
    sin_fecha = [d for d in con_mp if not (d.get("fecha_precio_ref") or "").strip()]
    clasif = Counter(clasificar_fila(d) for d in items)

    estados = Counter((f.get("estado") or "?") for f in facturas)
    completas = [f for f in facturas if (f.get("estado") or "").upper() == "COMPLETA"]
    parciales = [f for f in facturas if (f.get("estado") or "").upper() == "PARCIAL"]

    # MPs activos sin precio pero con ENTRADA en mov
    recuperables = []
    for d in sin_precio:
        mp = _norm_mp(d.get("cod_mp_sistema") or "")
        if mp in entradas_mp:
            recuperables.append((d, entradas_mp[mp]))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = (args.output or "").strip() or f"auditar_items_prov_{ts}.csv"

    print("=" * 60)
    print("BD_ITEMS_PROV")
    print("=" * 60)
    print(f"  Filas totales:        {len(items)}")
    print(f"  Activas (activo!=NO): {len(activos)}")
    print(f"  Con cod_mp_sistema:   {len(con_mp)}")
    print(f"  Sin precio_ref:       {len(sin_precio)}")
    print(f"  Sin fecha_precio_ref: {len(sin_fecha)}")
    print()
    print("  Clasificación por fila:")
    for k, v in clasif.most_common():
        print(f"    {k}: {v}")

    print()
    print("=" * 60)
    print("facturas_procesadas (Supabase)")
    print("=" * 60)
    print(f"  Total registros: {len(facturas)}")
    for k, v in estados.most_common():
        print(f"    {k}: {v}")
    if completas:
        print(
            f"  Última COMPLETA: {completas[0].get('num_factura')} "
            f"({str(completas[0].get('fecha_proceso') or '')[:10]})"
        )
    if parciales:
        print(f"  PARCIALES: {len(parciales)}")
        for f in parciales[:8]:
            print(
                f"    {f.get('num_factura')} match={f.get('items_procesados')} "
                f"sin_match={f.get('items_sin_match')}"
            )

    print()
    print("=" * 60)
    print("Recuperables (sin precio_ref pero con ENTRADA en mov)")
    print("=" * 60)
    print(f"  {len(recuperables)} ítems catálogo")
    for d, ent in recuperables[:15]:
        print(
            f"    {d.get('cod_item_prov')} MP{d.get('cod_mp_sistema')} "
            f"| mov {ent['fecha']} {ent['costo_u']:.6f}/gr? fact={ent['factura']}"
        )
    if len(recuperables) > 15:
        print(f"    ... +{len(recuperables)-15} más")

    # CSV detalle sin precio
    fieldnames = [
        "clasificacion",
        "cod_item_prov",
        "cod_proveedor",
        "cod_mp_sistema",
        "descripcion_proveedor",
        "precio_ref",
        "fecha_precio_ref",
        "factor_conversion",
        "unidad_compra",
        "activo",
        "tiene_entrada_mov",
        "ultima_entrada_fecha",
        "ultima_entrada_costo_u",
        "ultima_entrada_factura",
    ]
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for d in items:
            cl = clasificar_fila(d)
            if cl in ("ok", "inactivo"):
                continue
            mp = _norm_mp(d.get("cod_mp_sistema") or "")
            ent = entradas_mp.get(mp)
            w.writerow(
                {
                    "clasificacion": cl,
                    "cod_item_prov": d.get("cod_item_prov"),
                    "cod_proveedor": d.get("cod_proveedor"),
                    "cod_mp_sistema": d.get("cod_mp_sistema"),
                    "descripcion_proveedor": (d.get("descripcion_proveedor") or "")[:80],
                    "precio_ref": d.get("precio_ref"),
                    "fecha_precio_ref": d.get("fecha_precio_ref"),
                    "factor_conversion": d.get("factor_conversion"),
                    "unidad_compra": d.get("unidad_compra"),
                    "activo": d.get("activo"),
                    "tiene_entrada_mov": "SI" if ent else "NO",
                    "ultima_entrada_fecha": ent["fecha"] if ent else "",
                    "ultima_entrada_costo_u": ent["costo_u"] if ent else "",
                    "ultima_entrada_factura": ent["factura"] if ent else "",
                }
            )
    print()
    print(f"CSV problemas: {out_csv}")


if __name__ == "__main__":
    main()

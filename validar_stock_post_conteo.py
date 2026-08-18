"""
Valida stock post-conteo: baseline = conteo físico contabilizado;
movimientos desde contabilizado_at → stock teórico vs maestro.

Bodegas: BOD-001 (cocina), BOD-005 (externa).

Uso:
  python validar_stock_post_conteo.py
  python validar_stock_post_conteo.py --cod-mp 047 552
  python validar_stock_post_conteo.py --solo-lomo
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)

TIPOS_SUMA = {"AJUSTE_POSITIVO", "ENTRADA", "TRASLADO_ENTRADA"}
TIPOS_RESTA = {"SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRASLADO_SALIDA"}
BODEGAS = ("BOD-001", "BOD-005")
LOMO_MPS = frozenset({"047", "552"})


def _sb():
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _parse_ts(s: str) -> datetime:
    s = (s or "").strip()
    if s.endswith("+"):
        s = s[:-1]
    s = s.replace("Z", "")
    if "+" in s:
        s = s.split("+", 1)[0]
    if "T" not in s and s:
        s += "T00:00:00"
    s = s[:26]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(s[:19])


def ultimo_conteo_contabilizado(cod_bodega: str) -> dict | None:
    """Último ciclo CONTABILIZADO con el envío de más líneas (conteo completo)."""
    sb = _sb()
    ciclos = (
        sb.table("conteo_ciclo")
        .select("id,estado,created_at,snapshot_at,cod_bodega")
        .eq("cod_bodega", cod_bodega)
        .eq("estado", "CONTABILIZADO")
        .order("created_at", desc=True)
        .limit(10)
        .execute()
        .data
        or []
    )
    mejor: dict | None = None
    mejor_n = -1
    for ciclo in ciclos:
        envs = (
            sb.table("conteo_envio")
            .select("id,contabilizado_at,enviado_at,estado_aprobacion,secuencia")
            .eq("ciclo_id", ciclo["id"])
            .order("contabilizado_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not envs:
            continue
        envio = envs[0]
        lineas = (
            sb.table("conteo_envio_detalle")
            .select(
                "cod_mp_sistema,nombre_mp,unidad_base,conteo_fisico,"
                "stock_sistema_snapshot,delta_calculado"
            )
            .eq("envio_id", envio["id"])
            .execute()
            .data
            or []
        )
        n = len(lineas)
        if n > mejor_n:
            mejor_n = n
            mejor = {
                "cod_bodega": cod_bodega,
                "ciclo_id": ciclo["id"],
                "contabilizado_at": envio.get("contabilizado_at") or "",
                "envio_id": envio["id"],
                "lineas": lineas,
            }
    return mejor


def cargar_movs_desde(desde_iso: str) -> list[dict]:
    sb = _sb()
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("mov_inventario")
            .select(
                "fecha,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
                "cod_bodega_origen,cod_bodega_destino,origen_documento,num_documento"
            )
            .gte("fecha", desde_iso)
            .order("fecha")
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


def _bod_mov(m: dict) -> tuple[str, float]:
    tipo = (m.get("tipo_mov") or "").strip()
    cant = float(m.get("cantidad_mov") or 0)
    if tipo in TIPOS_SUMA:
        return (m.get("cod_bodega_destino") or "").strip(), cant
    if tipo in TIPOS_RESTA:
        return (m.get("cod_bodega_origen") or "").strip(), -cant
    return "", 0.0


def resumen_mov_post_conteo(
    movs: list[dict],
    *,
    cod_mp: str,
    cod_bodega: str,
    desde: datetime,
) -> tuple[dict[str, float], float]:
    buckets: dict[str, float] = defaultdict(float)
    neto = 0.0
    for m in movs:
        if (m.get("cod_mp_sistema") or "").strip() != cod_mp:
            continue
        try:
            f = _parse_ts(str(m.get("fecha") or ""))
        except ValueError:
            continue
        if f <= desde:
            continue
        bod, delta = _bod_mov(m)
        if bod != cod_bodega or delta == 0:
            continue
        neto += delta
        origen = (m.get("origen_documento") or "").strip().upper()
        tipo = (m.get("tipo_mov") or "").strip()
        if tipo == "ENTRADA" and origen == "FACTURA":
            key = "entradas_factura"
        elif tipo == "ENTRADA":
            key = "otras_entradas"
        elif tipo == "SALIDA_VENTA":
            key = "ventas"
        elif tipo.startswith("TRASLADO"):
            key = "traslados"
        elif tipo == "AJUSTE_NEGATIVO":
            key = "ajustes_neg"
        elif tipo == "AJUSTE_POSITIVO":
            key = "ajustes_pos"
        else:
            key = "otros"
        buckets[key] += delta
    return dict(buckets), round(neto, 4)


def stock_maestro(cod_mp: str | None = None) -> dict[tuple[str, str], float]:
    from google_credentials import google_credentials
    import gspread
    from sheet_numbers import parse_sheet_number

    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_MP_SISTEMA")
    rows = ws.get_all_values()
    hi = next(i for i, r in enumerate(rows) if "cod_mp_sistema" in r)
    hdr = [h.strip() for h in rows[hi]]
    ci = {h: i for i, h in enumerate(hdr)}
    out: dict[tuple[str, str], float] = {}
    for r in rows[hi + 1 :]:
        cod = r[ci["cod_mp_sistema"]] if len(r) > ci["cod_mp_sistema"] else ""
        bod = r[ci["cod_bodega"]] if len(r) > ci["cod_bodega"] else ""
        if not cod or not bod:
            continue
        if cod_mp and cod != cod_mp:
            continue
        if bod not in BODEGAS:
            continue
        out[(cod, bod)] = parse_sheet_number(
            r[ci.get("stock_actual", 0)] if "stock_actual" in ci else "0", 0.0
        )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cod-mp", nargs="*", help="Filtrar MPs")
    p.add_argument("--solo-lomo", action="store_true")
    p.add_argument("--tol", type=float, default=0.5, help="Tolerancia gr/ml/uni")
    args = p.parse_args()

    filtro: set[str] | None = None
    if args.solo_lomo:
        filtro = set(LOMO_MPS)
    elif args.cod_mp:
        filtro = set(args.cod_mp)

    from recalcular_stock_sheets import build_stock_calculado

    stock_calc = build_stock_calculado()
    stock_sheet = stock_maestro()

    conteos = []
    for bod in BODEGAS:
        info = ultimo_conteo_contabilizado(bod)
        if info:
            conteos.append(info)

    if not conteos:
        print("No hay conteos CONTABILIZADOS para BOD-001 / BOD-005")
        return 1

    print("=== BASELINE: último conteo contabilizado por bodega ===")
    desde_min = None
    for c in conteos:
        print(
            f"{c['cod_bodega']}: contabilizado {c['contabilizado_at'][:19]} | "
            f"{len(c['lineas'])} líneas"
        )
        ts = _parse_ts(c["contabilizado_at"])
        if desde_min is None or ts < desde_min:
            desde_min = ts

    movs = cargar_movs_desde(desde_min.isoformat())

    print("\n=== VALIDACION (conteo fisico + movs posteriores) ===")
    print(
        f"{'MP':<8} {'Bodega':<8} {'Conteo':>10} {'+Ent':>8} {'-Vent':>8} "
        f"{'Tras':>8} {'Aj':>8} {'Teorico':>10} {'Sheets':>10} {'Calc':>10} {'Diff':>8}"
    )

    desvios: list[dict] = []
    for c in conteos:
        bod = c["cod_bodega"]
        desde = _parse_ts(c["contabilizado_at"])
        for ln in c["lineas"]:
            mp = (ln.get("cod_mp_sistema") or "").strip()
            if not mp:
                continue
            if filtro and mp not in filtro:
                continue
            base = float(ln.get("conteo_fisico") or 0)
            buckets, neto = resumen_mov_post_conteo(movs, cod_mp=mp, cod_bodega=bod, desde=desde)
            entradas = buckets.get("entradas_factura", 0) + buckets.get("otras_entradas", 0)
            ventas = buckets.get("ventas", 0)
            traslados = buckets.get("traslados", 0)
            ajustes = buckets.get("ajustes_neg", 0) + buckets.get("ajustes_pos", 0)
            teorico = round(base + neto, 2)
            sheet = stock_sheet.get((mp, bod), 0.0)
            calc = stock_calc.get((mp, bod), 0.0)
            delta = round(teorico - calc, 2)
            if abs(delta) > args.tol or abs(teorico - sheet) > args.tol:
                desvios.append(
                    {
                        "mp": mp,
                        "bod": bod,
                        "nombre": ln.get("nombre_mp", ""),
                        "teorico": teorico,
                        "sheet": sheet,
                        "calc": calc,
                        "delta_calc": delta,
                    }
                )
            if filtro or abs(delta) > args.tol:
                print(
                    f"{mp:<8} {bod:<8} {base:>10.1f} {entradas:>+8.1f} {ventas:>+8.1f} "
                    f"{traslados:>+8.1f} {ajustes:>+8.1f} {teorico:>10.1f} "
                    f"{sheet:>10.1f} {calc:>10.1f} {delta:>+8.1f}"
                )

    print(f"\n=== DESVIOS > {args.tol} (teorico vs calc) ===")
    desvios.sort(key=lambda x: -abs(x["delta_calc"]))
    for d in desvios[:40]:
        print(
            f"  {d['mp']} {d['bod']} {d['nombre'][:30]:30} | "
            f"teórico={d['teorico']:.1f} sheets={d['sheet']:.1f} calc={d['calc']:.1f} "
            f"diff={d['delta_calc']:+.1f}"
        )
    print(f"Total desvios: {len(desvios)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

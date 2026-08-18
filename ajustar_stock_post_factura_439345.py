"""
Ajusta stock post factura 025-109-000439345:
  stock_objetivo = neto_ingresado_factura - consumo_posterior_al_ingreso

Corrige saldos cuando hubo descargo sin stock (ítems no inactivados).

Uso:
  python ajustar_stock_post_factura_439345.py --dry-run
  python ajustar_stock_post_factura_439345.py --produccion
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

NUM = "025-109-000439345"
TIPOS_SUMA = {"ENTRADA", "AJUSTE_POSITIVO", "TRASLADO_ENTRADA"}
TIPOS_RESTA = {"SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRASLADO_SALIDA"}


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]


def _norm_mp(mp: str) -> str:
    s = (mp or "").strip()
    return s.lstrip("0") or s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--produccion", action="store_true")
    args = ap.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2
    dry = args.dry_run

    from supabase import create_client
    from recalcular_stock_sheets import build_stock_calculado, paginar_todo

    movs = paginar_todo(
        "mov_inventario",
        "cod_mp_sistema,tipo_mov,cantidad_mov,cod_bodega_origen,cod_bodega_destino,"
        "fecha,num_documento,observaciones,costo_unitario,nombre_mp,unidad_base",
    )
    stock = build_stock_calculado(movs)

    entrada: dict[tuple[str, str], float] = defaultdict(float)
    since: dict[tuple[str, str], str] = defaultdict(lambda: "2026-07-01T00:00:00")
    nombres: dict[str, str] = {}

    for m in movs:
        mp = str(m.get("cod_mp_sistema") or "").strip()
        if mp:
            nombres[_norm_mp(mp)] = (m.get("nombre_mp") or nombres.get(_norm_mp(mp)) or "").strip()
        obs = m.get("observaciones") or ""
        if m.get("num_documento") != NUM:
            continue
        mp = str(m["cod_mp_sistema"]).strip()
        c = float(m["cantidad_mov"] or 0)
        if m["tipo_mov"] == "ENTRADA" and "REVERTIDO" not in obs:
            bod = m.get("cod_bodega_destino") or ""
            entrada[(mp, bod)] += c
            since[(mp, bod)] = max(since[(mp, bod)], m.get("fecha") or since[(mp, bod)])
        elif m["tipo_mov"] == "AJUSTE_NEGATIVO":
            bod = m.get("cod_bodega_origen") or ""
            entrada[(mp, bod)] -= c

    unidad_mp: dict[str, str] = {}
    for m in movs:
        mp = str(m.get("cod_mp_sistema") or "").strip()
        if mp and m.get("unidad_base"):
            unidad_mp[_norm_mp(mp)] = m["unidad_base"]

    def stock_actual(mp: str, bod: str) -> float:
        for (kmp, kbod), v in stock.items():
            if _norm_mp(kmp) == _norm_mp(mp) and kbod == bod:
                return float(v)
        return 0.0

    # Tajín clásico ingresó a BOD-005; en barra sigue MP534 con descargos sin stock previo.
    if entrada.get(("534", "BOD-005"), 0) > 0 and stock_actual("534", "BOD-002") < 0:
        entrada[("534", "BOD-002")] = entrada[("534", "BOD-005")]
        since[("534", "BOD-002")] = since[("534", "BOD-005")]

    def uso_posterior(mp: str, bod: str, desde: str) -> float:
        uso = 0.0
        for m in movs:
            if _norm_mp(str(m["cod_mp_sistema"])) != _norm_mp(mp):
                continue
            if (m.get("fecha") or "") < desde:
                continue
            t = m["tipo_mov"]
            c = float(m["cantidad_mov"] or 0)
            if m.get("num_documento") == NUM and t == "ENTRADA":
                continue
            if t in TIPOS_RESTA and m.get("cod_bodega_origen") == bod:
                uso += c
            elif t in TIPOS_SUMA and m.get("cod_bodega_destino") == bod:
                if m.get("num_documento") == NUM and t == "ENTRADA":
                    continue
                uso -= c
        return uso

    ajustes: list[dict] = []
    print(f"Factura {NUM} - ajustes stock (entrada factura - uso posterior):\n")
    for (mp, bod), ent in sorted(entrada.items()):
        if ent <= 0:
            continue
        uso = uso_posterior(mp, bod, since[(mp, bod)])
        objetivo = round(ent - uso, 4)
        actual = round(stock_actual(mp, bod), 4)
        delta = round(objetivo - actual, 4)
        if abs(delta) < 0.05:
            continue
        print(
            f"  MP{mp}@{bod}: entrada={ent} uso={uso:.2f} "
            f"objetivo={objetivo} actual={actual} -> ajuste {delta:+.2f}"
        )
        ajustes.append(
            {
                "mp": mp,
                "bod": bod,
                "delta": delta,
                "objetivo": objetivo,
                "nombre": nombres.get(_norm_mp(mp), ""),
            }
        )

    if not ajustes:
        print("Nada que ajustar.")
        return 0

    if dry:
        print(f"\n[DRY-RUN] {len(ajustes)} ajuste(s)")
        return 0

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    for a in ajustes:
        mp, bod, delta = a["mp"], a["bod"], a["delta"]
        cod = f"MOV-AJ439345-{_norm_mp(mp)}-{bod}-{_ts()}"
        if delta > 0:
            mov = {
                "cod_mov": cod,
                "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "tipo_mov": "AJUSTE_POSITIVO",
                "cod_mp_sistema": mp,
                "nombre_mp": a["nombre"],
                "cod_bodega_origen": None,
                "cod_bodega_destino": bod,
                "cantidad_mov": abs(delta),
                "unidad_base": unidad_mp.get(_norm_mp(mp), "gr"),
                "costo_unitario": None,
                "costo_total": None,
                "origen_documento": "AJUSTE_MANUAL",
                "num_documento": NUM,
                "registrado_por": "AGENTE",
                "observaciones": (
                    f"Ajuste post-factura: stock objetivo {a['objetivo']} "
                    f"(entrada - uso posterior) | factura {NUM}"
                ),
            }
        else:
            mov = {
                "cod_mov": cod,
                "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "tipo_mov": "AJUSTE_NEGATIVO",
                "cod_mp_sistema": mp,
                "nombre_mp": a["nombre"],
                "cod_bodega_origen": bod,
                "cod_bodega_destino": None,
                "cantidad_mov": abs(delta),
                "unidad_base": unidad_mp.get(_norm_mp(mp), "gr"),
                "costo_unitario": None,
                "costo_total": None,
                "origen_documento": "AJUSTE_MANUAL",
                "num_documento": NUM,
                "registrado_por": "AGENTE",
                "observaciones": (
                    f"Ajuste post-factura: stock objetivo {a['objetivo']} "
                    f"(entrada - uso posterior) | factura {NUM}"
                ),
            }
        sb.table("mov_inventario").insert(mov).execute()
        print(f"  OK {mov['tipo_mov']} MP{mp} {abs(delta)} @{bod}")

    print("\nRecalculando stock produccion...")
    subprocess.run(
        [sys.executable, "recalcular_stock_sheets.py", "--produccion"],
        cwd=os.path.dirname(__file__) or ".",
        check=True,
    )
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Diagnóstico: stock teórico por MP desde mov_inventario (misma lógica que recalcular_stock_sheets).

Uso (desde tatami-agente, venv activo):
  python auditar_stock_mov.py
  python auditar_stock_mov.py --top 40
  python auditar_stock_mov.py --cod 261

Ayuda a entender inventarios muy negativos:
  - más SALIDA_VENTA que ENTRADA en Supabase
  - tipos de movimiento ignorados por recalcular (no están en ENTRADA/SALIDA_VENTA/AJUSTE_*)
  - posibles duplicados (revisar si hay muchas filas con mismo num_documento+cod_mp)
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)

TIPOS_SUMA = {"AJUSTE_POSITIVO", "ENTRADA"}
TIPOS_RESTA = {"SALIDA_VENTA", "AJUSTE_NEGATIVO"}


def paginar(tabla: str, select: str) -> list:
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    rows = []
    offset = 0
    while True:
        chunk = sb.table(tabla).select(select).range(offset, offset + 999).execute().data
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def main():
    p = argparse.ArgumentParser(description="Auditoría stock desde mov_inventario")
    p.add_argument("--top", type=int, default=25, help="Cantidad de MPs más negativos a listar")
    p.add_argument("--cod", type=str, default="", help="Solo este cod_mp_sistema")
    args = p.parse_args()

    movs = paginar("mov_inventario", "cod_mp_sistema,tipo_mov,cantidad_mov,num_documento,cod_mov")
    print(f"Movimientos leídos: {len(movs)}")

    neg_qty = [m for m in movs if float(m.get("cantidad_mov") or 0) < 0]
    if neg_qty:
        print(f"\n*** ANOMALÍA: {len(neg_qty)} movimientos con cantidad_mov NEGATIVA (revierte signo en recálculo). ***")
        for m in neg_qty[:15]:
            print(
                f"    cod={m.get('cod_mp_sistema')} tipo={m.get('tipo_mov')} "
                f"cant={m.get('cantidad_mov')} doc={m.get('num_documento')} mov={m.get('cod_mov')}"
            )
        if len(neg_qty) > 15:
            print(f"    ... y {len(neg_qty) - 15} más")

    por_mp: dict[str, dict] = defaultdict(
        lambda: {
            "stock": 0.0,
            "n_ent": 0,
            "n_sal": 0,
            "sum_ent": 0.0,
            "sum_sal": 0.0,
            "otros": 0,
            "clave_salida": set(),
        }
    )

    for m in movs:
        cod = (m.get("cod_mp_sistema") or "").strip()
        if not cod:
            continue
        if args.cod and cod != args.cod.strip():
            continue
        tipo = (m.get("tipo_mov") or "").strip()
        try:
            cant = float(m.get("cantidad_mov") or 0)
        except (TypeError, ValueError):
            cant = 0.0
        d = por_mp[cod]
        if tipo in TIPOS_SUMA:
            d["stock"] += cant
            d["sum_ent"] += cant
            d["n_ent"] += 1
        elif tipo in TIPOS_RESTA:
            d["stock"] -= cant
            d["sum_sal"] += cant
            d["n_sal"] += 1
            if tipo == "SALIDA_VENTA":
                nd = (m.get("num_documento") or "").strip()
                if nd:
                    d["clave_salida"].add(f"{nd}|{cant:.4g}")
        else:
            d["otros"] += 1

    lista = sorted(por_mp.items(), key=lambda x: x[1]["stock"])
    if args.cod:
        lista = [(k, v) for k, v in lista if k == args.cod.strip()]
    else:
        lista = lista[: args.top]

    print("\nMP más negativos (stock = suma(ENTRADA+AJUSTE+) − suma(SALIDA+AJUSTE−); arranca en 0):\n")
    print(
        f"{'cod_mp':<8} {'stock':>14} {'n_+':>7} {'sum_+':>14} {'n_-':>7} {'sum_-':>14} {'otros':>6}"
        "\n         (n_+/sum_+ = movimientos que SUMAN al stock; incluye ENTRADA y AJUSTE_POSITIVO)"
    )
    for cod, d in lista:
        print(
            f"{cod:<8} {d['stock']:>14.2f} {d['n_ent']:>7} {d['sum_ent']:>14.2f} "
            f"{d['n_sal']:>7} {d['sum_sal']:>14.2f} {d['otros']:>6}"
        )

    # Tipos no contemplados en recalcular
    tipos_vistos: dict[str, int] = defaultdict(int)
    for m in movs:
        tipo = (m.get("tipo_mov") or "").strip()
        if tipo and tipo not in TIPOS_SUMA and tipo not in TIPOS_RESTA:
            tipos_vistos[tipo] += 1
    if tipos_vistos:
        print("\nWARN: movimientos con tipo_mov NO usado en recalcular_stock_sheets:")
        for t, n in sorted(tipos_vistos.items(), key=lambda x: -x[1]):
            print(f"   {t}: {n}")

    print(
        "\nNotas:"
        "\n  · Stock negativo fuerte suele ser: pocas ENTRADA vs muchas SALIDA_VENTA,"
        "\n    o inventario inicial nunca cargado como ENTRADA/AJUSTE_POSITIVO."
        "\n  · Revisar duplicados: limpiar_mov_duplicados.py (entradas factura)."
        "\n  · Revisar recetas: gramajes/cantidades que inflan consumo."
    )


if __name__ == "__main__":
    main()

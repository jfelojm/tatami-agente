"""Traslada lomo Piggis (552) de BOD-005 a BOD-001 y muestra inventario."""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv(override=True)

from inventario_stock_mp import norm_mp
from inventario_traslado import costo_ref_desde_filas_maestro, registrar_traslado_mp
from whatsapp_webhook import conectar_supabase, leer_bd_mp_sistema

ORIG = "BOD-005"
DEST = "BOD-001"
COD_MP = "552"


def fnum(v) -> float:
    if v is None or v == "":
        return 0.0
    s = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return float(v)


def stock_por_bodega(rows: list[dict], cod: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        if norm_mp(r.get("cod_mp_sistema")) != cod:
            continue
        bod = str(r.get("cod_bodega") or "").strip()
        out[bod] = fnum(r.get("stock_actual"))
    return out


def mostrar_lomo(rows: list[dict]) -> None:
    print("=== INVENTARIO LOMO ===")
    for cod in ("047", "552"):
        nombre = ""
        for r in rows:
            if norm_mp(r.get("cod_mp_sistema")) == cod:
                nombre = str(r.get("nombre_mp") or "").strip()
                break
        stocks = stock_por_bodega(rows, cod)
        total = sum(stocks.values())
        print(f"\n{cod} — {nombre}")
        for bod in sorted(stocks):
            print(f"  {bod}: {stocks[bod]:,.1f} g")
        print(f"  TOTAL: {total:,.1f} g")


def main() -> None:
    rows = leer_bd_mp_sistema(force_refresh=True)
    sb = conectar_supabase()

    stock_origen = 0.0
    stock_dest = 0.0
    nombre = "LOMO FINO DE RES PIGGIS"
    unidad = "gr"
    for r in rows:
        if norm_mp(r.get("cod_mp_sistema")) != COD_MP:
            continue
        bod = str(r.get("cod_bodega") or "").strip()
        if bod == ORIG:
            stock_origen = fnum(r.get("stock_actual"))
            nombre = str(r.get("nombre_mp") or nombre).strip()
            unidad = str(r.get("unidad_base") or "gr").strip()
        elif bod == DEST:
            stock_dest = fnum(r.get("stock_actual"))

    cantidad = round(stock_origen, 1)
    print("=== TRASLADO LOMO 005 -> 001 ===")
    print(f"{COD_MP} {nombre}")
    print(f"  Origen ({ORIG}): {stock_origen:,.1f} g")
    print(f"  Destino antes ({DEST}): {stock_dest:,.1f} g")
    print(f"  Cantidad a trasladar: {cantidad:,.1f} g")

    if cantidad <= 0:
        print("Sin stock en externa; no se registra traslado.")
    else:
        costo = costo_ref_desde_filas_maestro(rows, COD_MP, ORIG)
        mov = registrar_traslado_mp(
            sb,
            cod_mp=COD_MP,
            bodega_origen=ORIG,
            bodega_destino=DEST,
            cantidad=cantidad,
            nombre_mp=nombre,
            unidad_base=unidad,
            costo_unitario_ref=costo,
            registrado_por="AGENTE_TRASLADO_LOMO",
            recalcular_sheets=True,
        )
        print(f"  Registrado: {mov['cod_mov']}")

    print()
    rows_post = leer_bd_mp_sistema(force_refresh=True)
    mostrar_lomo(rows_post)


if __name__ == "__main__":
    main()

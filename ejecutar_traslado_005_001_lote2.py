"""Traslado BOD-005 -> BOD-001 (lote 2)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv

load_dotenv(override=True)

from inventario_stock_mp import norm_mp
from inventario_traslado import costo_ref_desde_filas_maestro, registrar_traslado_mp
from whatsapp_webhook import conectar_supabase, leer_bd_mp_sistema

ORIGEN, DESTINO = "BOD-005", "BOD-001"
TRASLADOS = [
    ("051", 2000, "gr"),
    ("SUB-008", 2000, "gr"),
    ("SUB-056", 5328, "gr"),
    ("007", 5000, "gr"),
    ("098", 1008, "gr"),
]


def filas_mp(rows, cod: str) -> list[dict]:
    t = norm_mp(cod)
    return [
        r
        for r in rows
        if norm_mp(r.get("cod_mp_sistema")) == t
        or (r.get("cod_mp_sistema") or "").strip().upper() == cod.upper()
    ]


def resolver_mp(rows, cod: str, unidad: str) -> dict:
    filas = filas_mp(rows, cod)
    if not filas:
        raise ValueError(f"MP {cod} no existe en BD_MP_SISTEMA")
    return {
        "cod_mp": (filas[0].get("cod_mp_sistema") or cod).strip(),
        "nombre_mp": (filas[0].get("nombre_mp") or cod).strip(),
        "unidad_base": (filas[0].get("unidad_base") or unidad or "gr").strip() or unidad,
    }


def main() -> None:
    sb = conectar_supabase()
    rows = leer_bd_mp_sistema(force_refresh=True)
    ok = 0
    print(f"Traslados {ORIGEN} -> {DESTINO}\n")
    for cod, cant, unidad in TRASLADOS:
        info = resolver_mp(rows, cod, unidad)
        costo = costo_ref_desde_filas_maestro(rows, info["cod_mp"], ORIGEN)
        if costo <= 0:
            costo = costo_ref_desde_filas_maestro(rows, info["cod_mp"], DESTINO)
        stock_005 = next(
            (
                r.get("stock_actual")
                for r in filas_mp(rows, cod)
                if (r.get("cod_bodega") or "").strip() == ORIGEN
            ),
            None,
        )
        res = registrar_traslado_mp(
            sb,
            cod_mp=info["cod_mp"],
            bodega_origen=ORIGEN,
            bodega_destino=DESTINO,
            cantidad=float(cant),
            nombre_mp=info["nombre_mp"],
            unidad_base=info["unidad_base"],
            costo_unitario_ref=costo,
            registrado_por="TRASLADO_MANUAL_CURSOR",
            recalcular_sheets=True,
        )
        print(
            f"OK  MP{info['cod_mp']} {info['nombre_mp']}: "
            f"{cant} {info['unidad_base']} | stock_005_antes={stock_005} | mov={res['cod_mov']}"
        )
        ok += 1
    print(f"\nResumen: {ok}/{len(TRASLADOS)} OK")


if __name__ == "__main__":
    main()

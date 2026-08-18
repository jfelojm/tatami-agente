"""Re-ejecuta las 9 líneas ERROR de TRA-20260625-233039 (BOD-005 → BOD-001)."""
from dotenv import load_dotenv

load_dotenv(override=True)

from inventario_stock_mp import norm_mp
from inventario_traslado import costo_ref_desde_filas_maestro, registrar_traslado_mp
from traslado_masivo_routes import _extraer_cod_producto
from whatsapp_webhook import _resolver_mp_por_nombre, conectar_supabase, leer_bd_mp_sistema

LINEAS = [
    ("MANTEQUILLA S/S — 87 — 1010.3611 — gr", 360),
    ("CREMA DE LECHE — 019 — 12672.18 — gr", 1800),
    ("LOMO FINO DE RES ITALIANA — 047 — 14672.1687 — gr", 5400),
    ("LOMO FINO DE RES PIGGIS — 552 — 11162.0481 — gr", 2000),
    ("pollo en salmuera — SUB-008 — 1000 — gr", 3500),
    ("SALMON — 127 — 6496.2527 — gr", 4540),
    ("SALSA DE OSTRAS — 081 — 152972.61 — gr", 4540),
    ("FIDEO DE ARROZ PLANO — 097 — 38783.133 — gr", 10000),
    ("torta de chocolate — SUB-061 — 1054 — gr", 1054),
]

ORIG = "BOD-005"
DEST = "BOD-001"


def main():
    rows = leer_bd_mp_sistema(force_refresh=True)
    sb = conectar_supabase()
    ok, err = 0, 0
    cods_ok: set[str] = set()

    for idx, (prod, cant) in enumerate(LINEAS, start=1):
        cod = _extraer_cod_producto(prod)
        res = _resolver_mp_por_nombre(
            rows, nombre_mp=prod, cod_mp=cod, bodega_origen=ORIG
        )
        if not res.get("ok"):
            print(f"ERR {idx:02d} NO_RESUELTO {prod[:40]} — {res.get('error') or res.get('mensaje')}")
            err += 1
            continue

        cod_mp = res["cod_mp"]
        nombre = res.get("nombre_mp") or cod_mp
        costo = costo_ref_desde_filas_maestro(rows, cod_mp, ORIG)
        unidad = "gr"
        for r in rows:
            if norm_mp(r.get("cod_mp_sistema")) == norm_mp(cod_mp):
                unidad = str(r.get("unidad_base") or "gr").strip() or "gr"
                break
        if str(cod_mp).upper().startswith("SUB-"):
            unidad = res.get("unidad_base") or unidad

        try:
            mov = registrar_traslado_mp(
                sb,
                cod_mp=cod_mp,
                bodega_origen=ORIG,
                bodega_destino=DEST,
                cantidad=float(cant),
                nombre_mp=nombre,
                unidad_base=unidad,
                costo_unitario_ref=costo,
                registrado_por="RETRY:TRA-20260625-233039",
                recalcular_sheets=False,
                secuencia=idx,
            )
            print(f"OK  {idx:02d} {cod_mp:8} {nombre[:35]:35} {cant} -> {mov['cod_mov']}")
            ok += 1
            cods_ok.add(norm_mp(cod_mp))
        except Exception as e:
            print(f"ERR {idx:02d} {cod_mp:8} {prod[:35]} — {e}")
            err += 1

    if cods_ok:
        from recalcular_stock_sheets import recalcular_produccion

        for cod in sorted(cods_ok):
            recalcular_produccion(cod_mp_filtro=cod)

    print(f"\nResultado: {ok} OK, {err} error")


if __name__ == "__main__":
    main()

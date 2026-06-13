"""Trazabilidad MP 268: conteos vs movimientos (jun 2026)."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from supabase import create_client

from recalcular_stock_sheets import TIPOS_RESTA_ORIGEN, TIPOS_SUMA_DESTINO, _bodega_mov, _clave_stock

load_dotenv(override=True)

COD = "268"
BOD = "BOD-002"


def paginar(sb, tabla, select, filtros=None):
    rows, off = [], 0
    while True:
        q = sb.table(tabla).select(select)
        if filtros:
            for op, col, val in filtros:
                q = getattr(q, op)(col, val)
        chunk = q.range(off, off + 999).execute().data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        off += 1000
    return rows


def saldo_hasta(movs_sorted, cod_mov_limite: str | None = None, antes_de_fecha: str | None = None) -> float:
    k = _clave_stock(COD, BOD)
    s = 0.0
    for m in movs_sorted:
        cm = m.get("cod_mov", "")
        if cod_mov_limite and cm == cod_mov_limite:
            break
        if antes_de_fecha and str(m.get("fecha") or "")[:19] >= antes_de_fecha[:19]:
            break
        if (m.get("cod_mp_sistema") or "").strip().lstrip("0") not in (COD, COD.lstrip("0")):
            continue
        tipo = (m.get("tipo_mov") or "").strip()
        bod = _bodega_mov(m, tipo)
        if _clave_stock(COD, bod) != k:
            continue
        cant = float(m.get("cantidad_mov") or 0)
        if tipo in TIPOS_SUMA_DESTINO:
            s += cant
        elif tipo in TIPOS_RESTA_ORIGEN:
            s -= cant
    return round(s, 4)


def main() -> None:
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    movs = paginar(
        sb,
        "mov_inventario",
        "cod_mov,fecha,tipo_mov,cantidad_mov,cod_mp_sistema,cod_bodega_origen,cod_bodega_destino,"
        "origen_documento,num_documento,observaciones,creado_en",
        [("eq", "cod_mp_sistema", COD)],
    )
    movs_bod = [
        m
        for m in movs
        if BOD
        in (
            (m.get("cod_bodega_origen") or "").strip(),
            (m.get("cod_bodega_destino") or "").strip(),
        )
    ]
    movs_bod.sort(key=lambda m: (str(m.get("fecha") or ""), str(m.get("cod_mov") or "")))

    print("=" * 72)
    print("MP 268 Guitig — TRAZABILIDAD CONTEOS vs MOV (BOD-002)")
    print("=" * 72)

    # Conteos contabilizados
    dets = paginar(
        sb,
        "conteo_envio_detalle",
        "envio_id,conteo_fisico,stock_sistema_snapshot,delta_calculado,cod_mov_ajuste,"
        "estado_linea,cod_bodega,created_at",
        [("eq", "cod_mp_sistema", COD)],
    )
    conteos = [
        d
        for d in dets
        if (d.get("cod_bodega") or "").strip() == BOD and d.get("cod_mov_ajuste")
    ]

    for d in sorted(conteos, key=lambda x: x.get("cod_mov_ajuste", "")):
        cod_mov = d["cod_mov_ajuste"]
        mov = next((m for m in movs_bod if m["cod_mov"] == cod_mov), None)
        fecha_mov = str(mov.get("fecha") or "")[:19] if mov else "?"
        saldo_antes = saldo_hasta(movs_bod, cod_mov)
        conteo = float(d["conteo_fisico"])
        snap = float(d["stock_sistema_snapshot"] or 0)
        delta_reg = float(d["delta_calculado"])
        delta_ok = round(conteo - saldo_antes, 4)
        saldo_despues = round(saldo_antes + delta_reg, 4)
        saldo_deberia = conteo

        print(f"\n--- Conteo mov={cod_mov} fecha={fecha_mov} ---")
        print(f"  Conteo físico reportado:     {conteo}")
        print(f"  Snapshot en detalle:       {snap}  (Sheets al registrar envío)")
        print(f"  Saldo MOV justo antes:       {saldo_antes}")
        print(f"  Delta REGISTRADO:            {delta_reg:+}  (= conteo − snapshot)")
        print(f"  Delta CORRECTO (vs mov):     {delta_ok:+}  (= conteo − saldo_mov)")
        print(f"  Saldo tras ajuste aplicado:  {saldo_despues}  (debería ser {saldo_deberia})")
        print(f"  ERROR en saldo final:        {saldo_despues - saldo_deberia:+.1f} uni")

    # Ventana 11-jun alrededor del conteo
    print("\n" + "=" * 72)
    print("MOVIMIENTOS 11-JUN-2026 (contexto conteo 22:54)")
    print("=" * 72)
    s = saldo_hasta(movs_bod, antes_de_fecha="2026-06-11T00:00:00")
    print(f"Saldo inicio 11-jun: {s}\n")
    for m in movs_bod:
        f = str(m.get("fecha") or "")[:19]
        if not f.startswith("2026-06-11") and not f.startswith("2026-06-12"):
            continue
        tipo = (m.get("tipo_mov") or "").strip()
        cant = float(m.get("cantidad_mov") or 0)
        orig = (m.get("cod_bodega_origen") or "").strip()
        dest = (m.get("cod_bodega_destino") or "").strip()
        if tipo in ("ENTRADA", "AJUSTE_POSITIVO", "TRASLADO_ENTRADA") and dest == BOD:
            delta = cant
        elif tipo in ("SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRASLADO_SALIDA") and orig == BOD:
            delta = -cant
        else:
            continue
        s += delta
        marca = ""
        if "CONTEO" in (m.get("cod_mov") or ""):
            marca = " <<< CONTEO"
        print(f"  {f} {tipo:16} {delta:+6.1f}  saldo={s:6.1f}  {m.get('cod_mov','')[:50]}{marca}")

    print(f"\nSaldo final hoy (suma mov): {saldo_hasta(movs_bod):.1f}")

    # conteo_linea snapshot vs mov en momento conteo 11-jun
    envio_id = None
    for d in conteos:
        if "60103ee2" in (d.get("cod_mov_ajuste") or ""):
            envio_id = d.get("envio_id")
            break
    if envio_id:
        env = (
            sb.table("conteo_envio")
            .select("id,ciclo_id,secuencia,enviado_at,contabilizado_at")
            .eq("id", envio_id)
            .limit(1)
            .execute()
            .data
            or [None]
        )[0]
        print("\n--- Envío conteo 11-jun ---")
        if env:
            print(f"  envio_id: {env.get('id')}")
            print(f"  enviado_at: {env.get('enviado_at')}")
            print(f"  contabilizado_at: {env.get('contabilizado_at')}")
            print(f"  secuencia: {env.get('secuencia')}")


if __name__ == "__main__":
    main()

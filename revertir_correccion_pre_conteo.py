"""
Restaura descargos eliminados por error ANTES del conteo 29-may-2026.

La corrección amplia (desde 2026-03-01) no debía tocar ventas pre-conteo:
el conteo del 29-may ya fijó la foto física. Este script:
  1. Re-descarga ventas pre-conteo cuyos movimientos se borraron.
  2. Restaura ajuste legacy MOV-20260508-113 si falta.
  3. Recalcula stock.

Uso:
  python revertir_correccion_pre_conteo.py
  python revertir_correccion_pre_conteo.py --produccion --recalcular-stock
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
FECHA_CONTEO = "2026-05-29"
LOG_CORRECCION = ROOT / "logs" / "correccion_prefacturas_barra.json"

AJUSTE_LEGACY = {
    "cod_mov": "MOV-20260508-113",
    "fecha": "2026-05-08T00:00:00",
    "tipo_mov": "AJUSTE_POSITIVO",
    "cod_mp_sistema": "268",
    "nombre_mp": "Guitig Vidrio",
    "cod_bodega_origen": "BOD-002",
    "cod_bodega_destino": "BOD-002",
    "cantidad_mov": -210.0,
    "unidad_base": "uni",
    "origen_documento": "INVENTARIO_FISICO",
}


def _ventas_rango(sb, desde: str, hasta: str) -> list[dict]:
    rows: list[dict] = []
    off = 0
    while True:
        q = (
            sb.table("hist_ventas")
            .select("*")
            .eq("estado_match", "PROCESADO")
            .gte("fecha", desde)
            .lte("fecha", hasta)
            .range(off, off + 999)
            .execute()
        )
        c = q.data or []
        rows.extend(c)
        if len(c) < 1000:
            break
        off += 1000
    return rows


def _cod_ventas_sin_mov(sb, cod_ventas: list[str]) -> list[str]:
    sin_mov: list[str] = []
    for i in range(0, len(cod_ventas), 100):
        chunk = cod_ventas[i : i + 100]
        for cv in chunk:
            r = (
                sb.table("mov_inventario")
                .select("cod_mov")
                .eq("num_documento", cv)
                .eq("tipo_mov", "SALIDA_VENTA")
                .limit(1)
                .execute()
                .data
                or []
            )
            if not r:
                sin_mov.append(cv)
    return sin_mov


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--recalcular-stock", action="store_true")
    args = p.parse_args()
    dry = not args.produccion

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    print("=" * 70)
    print(f"RESTAURAR PRE-CONTEO (<={FECHA_CONTEO}) — {'DRY RUN' if dry else 'PRODUCCIÓN'}")
    print("=" * 70)

    # Ventas clasificadas como omit en el rango ampliado (pre-conteo)
    from ventas_documento_prefactura import clasificar_ventas

    ventas = _ventas_rango(sb, "2026-03-01", FECHA_CONTEO)
    omit, _ = clasificar_ventas(ventas)
    cod_omit_pre = list(omit.keys())
    print(f"Ventas omit (pre-conteo) en clasificación: {len(cod_omit_pre)}")

    sin_mov = _cod_ventas_sin_mov(sb, cod_omit_pre)
    print(f"Sin mov_inventario (a re-descargar): {len(sin_mov)}")

    legacy_falta = not (
        sb.table("mov_inventario")
        .select("cod_mov")
        .eq("cod_mov", AJUSTE_LEGACY["cod_mov"])
        .limit(1)
        .execute()
        .data
    )
    print(f"Ajuste legacy MOV-20260508-113 falta: {legacy_falta}")

    if dry:
        print("\n[DRY RUN] No se aplican cambios.")
        if sin_mov[:10]:
            print("Ejemplos re-descargo:", sin_mov[:10])
        return 0

    if sin_mov:
        print(f"\nRe-descargando {len(sin_mov)} ventas pre-conteo (sin filtro prefactura)…")
        os.environ["DESCARGO_IGNORAR_PREFACTURA"] = "1"
        for cv in sin_mov:
            sb.table("hist_ventas").update({"descargado": False}).eq("cod_venta", cv).execute()

        fechas = sorted({str(v.get("fecha") or "")[:10] for v in ventas if v.get("cod_venta") in sin_mov})
        from descargo_inventario import procesar_descargo

        for f in fechas:
            if f <= FECHA_CONTEO:
                print(f"  descargo {f}…")
                procesar_descargo(f)
        os.environ.pop("DESCARGO_IGNORAR_PREFACTURA", None)

    if legacy_falta:
        print("\nRestaurando MOV-20260508-113…")
        row = {**AJUSTE_LEGACY, "creado_en": datetime.now().isoformat()}
        sb.table("mov_inventario").insert(row).execute()

    if args.recalcular_stock:
        print("\nRecalculando stock…")
        subprocess.run(
            [sys.executable, str(ROOT / "recalcular_stock_sheets.py"), "--produccion"],
            cwd=str(ROOT),
            check=False,
        )

    out = ROOT / "logs" / "restaurar_pre_conteo.json"
    out.write_text(
        json.dumps(
            {
                "fecha": datetime.now().isoformat(),
                "redescargadas": sin_mov,
                "legacy_restaurado": legacy_falta,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Log: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
V0ALC1 Sangre de Toro Blanco: vende por botella (uni), no por ml.

Corrige:
  - BD_MP_SISTEMA: unidad_base uni, tipo_control UNIDAD, costo por botella
  - BD_RECETAS_DETALLE receta 176 var SangreDeToroBlanco: cantidad 60 → 1
  - mov_inventario: descargo y ajuste conteo coherente con 1 botella
  - recalcular stock Sheets

Uso:
  python ajustar_v0alc1_unidades.py
  python ajustar_v0alc1_unidades.py --produccion --recalcular-par
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client
from google_credentials import google_credentials

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
COD_MP = "V0ALC1"
BODEGA = "BOD-002"
COSTO_ML = 0.0093
ML_BOTELLA = 750.0
COSTO_UNI = round(COSTO_ML * ML_BOTELLA, 4)  # 6.975

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _sheet():
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _header_idx(values: list[list[str]], key: str) -> tuple[int, list[str]]:
    hi = next(i for i, r in enumerate(values) if key in [(c or "").strip() for c in r])
    return hi, [(c or "").strip() for c in values[hi]]


def ajustar_maestro(*, produccion: bool) -> None:
    ws = _sheet().worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi, h = _header_idx(vals, "cod_mp_sistema")
    ic = h.index("cod_mp_sistema")
    iu = h.index("unidad_base")
    it = h.index("tipo_control")
    icu = h.index("costo_unitario_ref")

    row_1 = None
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        cod = (row[ic] if ic < len(row) else "").strip().upper()
        if cod == COD_MP:
            row_1 = i
            break
    if not row_1:
        raise SystemExit(f"No se encontró {COD_MP} en BD_MP_SISTEMA")

    updates = [
        {"range": rowcol_to_a1(row_1, iu + 1), "values": [["uni"]]},
        {"range": rowcol_to_a1(row_1, it + 1), "values": [["UNIDAD"]]},
        {"range": rowcol_to_a1(row_1, icu + 1), "values": [[str(COSTO_UNI).replace(".", ",")]]},
    ]
    print(f"BD_MP_SISTEMA fila {row_1}: unidad_base=uni, tipo_control=UNIDAD, costo={COSTO_UNI} USD/uni")
    if produccion:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)


def ajustar_receta(*, produccion: bool) -> None:
    ws = _sheet().worksheet("BD_RECETAS_DETALLE")
    vals = ws.get_all_values()
    hi, h = _header_idx(vals, "cod_receta")
    icr = h.index("cod_receta")
    iv = h.index("variedad_smart_menu")
    imp = h.index("cod_mp_sistema")
    iq = h.index("cantidad")

    targets: list[int] = []
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        rec = (row[icr] if icr < len(row) else "").strip()
        var = (row[iv] if iv < len(row) else "").strip()
        mp = (row[imp] if imp < len(row) else "").strip().upper()
        qty = (row[iq] if iq < len(row) else "").strip()
        if rec == "176" and var == "SangreDeToroBlanco" and mp == COD_MP and qty != "1":
            targets.append(i)

    if not targets:
        print("BD_RECETAS_DETALLE: receta 176 SangreDeToroBlanco ya en cantidad=1")
        return

    updates = [{"range": rowcol_to_a1(r, iq + 1), "values": [["1"]]} for r in targets]
    print(f"BD_RECETAS_DETALLE: cantidad 60→1 en fila(s) {targets}")
    if produccion:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)


def ajustar_movimientos(sb, *, produccion: bool) -> None:
    # Descargo erróneo (60 uni por receta mal configurada) → 1 botella
    sale = (
        sb.table("mov_inventario")
        .select("cod_mov,cantidad_mov")
        .eq("cod_mov", "MOV-20260425-V0ALC1-be095d4fb68442e5")
        .execute()
        .data
        or []
    )
    if sale and float(sale[0].get("cantidad_mov") or 0) != 1.0:
        print("mov SALIDA_VENTA: cantidad_mov 60 → 1 uni")
        if produccion:
            sb.table("mov_inventario").update({"cantidad_mov": 1.0, "unidad_base": "uni"}).eq(
                "cod_mov", "MOV-20260425-V0ALC1-be095d4fb68442e5"
            ).execute()

    # Conteo ingresó 750 ml (=1 botella) como 750 ml → delta 810; corregir a delta 2 (stock -1 → 1)
    conteo = (
        sb.table("mov_inventario")
        .select("cod_mov,cantidad_mov,observaciones")
        .eq("cod_mov", "MOV-CONTEO+d9663c82-V0ALC1-20260529184623754")
        .execute()
        .data
        or []
    )
    if conteo and float(conteo[0].get("cantidad_mov") or 0) != 2.0:
        obs = (conteo[0].get("observaciones") or "") + " | corregido: 1 botella=1 uni (antes 750 ml)"
        print("mov CONTEO: cantidad_mov 810 ml → 2 uni (1 botella contada)")
        if produccion:
            sb.table("mov_inventario").update(
                {
                    "cantidad_mov": 2.0,
                    "unidad_base": "uni",
                    "observaciones": obs.strip(" |"),
                }
            ).eq("cod_mov", "MOV-CONTEO+d9663c82-V0ALC1-20260529184623754").execute()

    # Ajuste legacy con unidad coherente
    legacy = (
        sb.table("mov_inventario")
        .select("cod_mov,unidad_base")
        .eq("cod_mov", "MOV-20260508-247")
        .execute()
        .data
        or []
    )
    if legacy and (legacy[0].get("unidad_base") or "") != "uni":
        if produccion:
            sb.table("mov_inventario").update({"unidad_base": "uni"}).eq(
                "cod_mov", "MOV-20260508-247"
            ).execute()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--recalcular-par", action="store_true")
    args = p.parse_args()
    dry = not args.produccion

    print("=" * 60)
    print(f"AJUSTE V0ALC1 → uni {'DRY RUN' if dry else 'PRODUCCIÓN'}")
    print("=" * 60)

    ajustar_maestro(produccion=args.produccion)
    ajustar_receta(produccion=args.produccion)

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    ajustar_movimientos(sb, produccion=args.produccion)

    if args.produccion:
        print("\nRecalculando stock Sheets…")
        rc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "recalcular_stock_sheets.py"),
                "--produccion",
                "--cod-mp",
                COD_MP,
            ],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        ).returncode
        if rc != 0:
            print(f"WARN recalcular_stock exit={rc}")
            return rc

        if args.recalcular_par:
            print("Recalculando PAR…")
            subprocess.run(
                [sys.executable, str(ROOT / "calcular_par_levels.py"), "--produccion"],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

    # Verificar saldo
    movs = (
        sb.table("mov_inventario")
        .select("tipo_mov,cantidad_mov,cod_bodega_origen,cod_bodega_destino")
        .eq("cod_mp_sistema", COD_MP)
        .execute()
        .data
        or []
    )
    stock = 0.0
    for m in movs:
        t = m.get("tipo_mov") or ""
        c = float(m.get("cantidad_mov") or 0)
        if t in ("AJUSTE_POSITIVO", "ENTRADA", "TRASLADO_ENTRADA"):
            stock += c
        elif t in ("SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRASLADO_SALIDA"):
            stock -= c
    print(f"\nSaldo ledger V0ALC1: {stock:.2f} uni (esperado: 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

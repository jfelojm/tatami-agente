"""Descargo manual insumos torta desde BOD-005."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google_credentials import google_credentials
from supabase import create_client

from recalcular_stock_sheets import _clave_stock, build_stock_calculado
from sheet_numbers import parse_sheet_number

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
BOD = "BOD-005"

LINEAS = [
    ("87", "MANTEQUILLA S/S", 300.0, "gr"),
    ("128", "CHOCOLATE NEGRO 56%", 450.0, "gr"),
    ("069", "HUEVOS", 6.0, "uni"),
    ("005", "AZUCAR", 540.0, "gr"),
    ("574", "CACAO EN POLVO", 54.0, "gr"),
    ("001", "HARINA", 270.0, "gr"),
    ("017", "SAL", 5.0, "gr"),
]


def _norm_mp(c: str) -> str:
    s = (c or "").strip().lstrip("0") or "0"
    return s


def _cod_hoja(c: str) -> str:
    nk = _norm_mp(c)
    return nk.zfill(3) if nk.isdigit() else nk


def _stock_mp(stock: dict, cod: str) -> float:
    for key in (cod, _norm_mp(cod), _cod_hoja(cod)):
        v = stock.get(_clave_stock(key, BOD))
        if v is not None:
            return float(v)
    return 0.0


def main() -> int:
    import gspread

    sh = gspread.authorize(
        google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    ).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_MP_SISTEMA")
    rows = ws.get_all_values()
    hi = next(i for i, r in enumerate(rows) if "cod_mp_sistema" in r)
    hdr = [h.strip() for h in rows[hi]]
    ci = {h: i for i, h in enumerate(hdr)}

    costos: dict[str, dict] = {}
    for r in rows[hi + 1 :]:
        cod = _norm_mp(r[ci["cod_mp_sistema"]] if ci["cod_mp_sistema"] < len(r) else "")
        bod = (r[ci["cod_bodega"]] if ci["cod_bodega"] < len(r) else "").strip()
        if bod != BOD:
            continue
        costos[cod] = {
            "nombre": r[ci["nombre_mp"]] if ci["nombre_mp"] < len(r) else "",
            "cu": parse_sheet_number(
                r[ci["costo_unitario_ref"]] if ci["costo_unitario_ref"] < len(r) else 0
            ),
            "unidad": (
                r[ci["unidad_base"]] if ci["unidad_base"] < len(r) else "gr"
            ).strip(),
        }

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    stock = build_stock_calculado()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    doc = f"DESC-BOD005-TORTA-{ts[:8]}"
    movs: list[dict] = []

    print(f"DESCARGO {BOD} — insumos torta de chocolate")
    print(f"Documento: {doc}\n")
    print(f"{'MP':<6} {'Nombre':<24} {'Cant':>8} {'Stock':>10}  {'Nuevo':>10}")
    print("-" * 65)

    for cod_raw, nom_default, cant, uni_default in LINEAS:
        nk = _norm_mp(cod_raw)
        info = costos.get(nk, {})
        cu = float(info.get("cu") or 0)
        unidad = info.get("unidad") or uni_default
        nombre = info.get("nombre") or nom_default
        cod_mp = _cod_hoja(cod_raw)
        antes = _stock_mp(stock, cod_raw)
        despues = antes - cant

        movs.append(
            {
                "cod_mov": f"MOV-DESC-{ts}-{nk}-{uuid.uuid4().hex[:10]}",
                "fecha": fecha,
                "tipo_mov": "AJUSTE_NEGATIVO",
                "cod_mp_sistema": cod_mp,
                "nombre_mp": nombre,
                "cod_bodega_origen": BOD,
                "cod_bodega_destino": None,
                "cantidad_mov": cant,
                "unidad_base": unidad,
                "costo_unitario": cu,
                "costo_total": round(cant * cu, 4),
                "origen_documento": "AJUSTE_MANUAL",
                "num_documento": doc,
                "registrado_por": "AGENTE",
                "observaciones": "Descargo manual BOD-005 insumos torta de chocolate",
            }
        )
        print(
            f"{cod_mp:<6} {nombre[:24]:<24} {cant:>8.1f} {antes:>10.1f}  {despues:>10.1f} {unidad}"
        )

    sb.table("mov_inventario").insert(movs).execute()
    print(f"\nInsertados {len(movs)} movimientos AJUSTE_NEGATIVO.")

    cods = sorted({_cod_hoja(c) for c, _, _, _ in LINEAS})
    for cod_mp in cods:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "recalcular_stock_sheets.py"),
                "--produccion",
                "--cod-mp",
                cod_mp,
            ],
            check=False,
        )
    print("Stocks recalculados en BD_MP_SISTEMA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

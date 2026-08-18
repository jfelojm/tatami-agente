"""
Actualiza BD_ITEMS_PROV.cod_bodega_destino según área del proveedor:

  BOD-002 — proveedores Tipo Barra en BD_PROV
  BOD-005 — proveedores Tipo Cocina (regla desde jun-2026)
  BOD-001 — excepciones cocina en restaurante: 161 Sumba, 172 LA MADRE

Si BD_PROV no tiene Tipo: mantiene BOD-002 si ya era barra; resto cocina → BOD-005.

Uso:
  python aplicar_bodega_ingreso_catalogo.py --dry-run
  python aplicar_bodega_ingreso_catalogo.py --produccion
"""
from __future__ import annotations

import argparse
import os
import time

import gspread
from dotenv import load_dotenv
from gspread.utils import rowcol_to_a1

from bodegas_config import normalizar_cod_bodega
from google_credentials import google_credentials
import procesar_facturas_drive as pfd

load_dotenv(override=True)

SHEET = "BD_ITEMS_PROV"
PROV_INGRESO_BOD_001 = frozenset({"161", "172"})


def cargar_prov_tipo(sh) -> dict[str, str]:
    vals = sh.worksheet("BD_PROV").get_all_values()
    if not vals:
        return {}
    headers = [str(h or "").strip().lower() for h in vals[0]]
    try:
        icod = headers.index("cod_proveedor")
    except ValueError:
        return {}
    itipo = headers.index("tipo") if "tipo" in headers else None
    out: dict[str, str] = {}
    for row in vals[1:]:
        cod = (row[icod] if icod < len(row) else "").strip()
        tipo = ""
        if itipo is not None and itipo < len(row):
            tipo = (row[itipo] or "").strip().upper()
        if cod:
            out[cod] = tipo
    return out


def target_bodega(cod_proveedor: str, tipo_prov: str, bodega_actual: str) -> str:
    prov = (cod_proveedor or "").strip()
    if prov in PROV_INGRESO_BOD_001:
        return "BOD-001"
    tipo = (tipo_prov or "").strip().upper()
    if tipo == "BARRA":
        return "BOD-002"
    if tipo == "COCINA":
        return "BOD-005"
    actual = normalizar_cod_bodega(bodega_actual)
    if actual == "BOD-002":
        return "BOD-002"
    if actual == "BOD-001" and prov in PROV_INGRESO_BOD_001:
        return "BOD-001"
    return "BOD-005"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--produccion", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dry = not args.produccion or args.dry_run

    sh = gspread.authorize(
        google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    ).open_by_key(os.environ["SPREADSHEET_ID"])
    prov_tipo = cargar_prov_tipo(sh)
    ws = sh.worksheet(SHEET)
    vals = ws.get_all_values()
    hi = next(
        i
        for i, row in enumerate(vals)
        if any((c or "").strip() == "cod_item_prov" for c in row)
    )
    headers = [(c or "").strip() for c in vals[hi]]
    try:
        icod_prov = headers.index("cod_proveedor")
        ibod = headers.index("cod_bodega_destino")
    except ValueError as e:
        print(f"ERROR columnas: {e}")
        return 1

    updates: list[dict] = []
    stats = {"sin_cambio": 0, "a_005": 0, "a_002": 0, "a_001": 0}

    for r_idx, row in enumerate(vals[hi + 1 :], start=hi + 2):
        if not row or not any((c or "").strip() for c in row):
            continue
        prov = (row[icod_prov] if icod_prov < len(row) else "").strip()
        actual = (row[ibod] if ibod < len(row) else "").strip()
        nuevo = target_bodega(prov, prov_tipo.get(prov, ""), actual)
        if nuevo == actual:
            stats["sin_cambio"] += 1
            continue
        stats[f"a_{nuevo[-3:]}"] = stats.get(f"a_{nuevo[-3:]}", 0) + 1
        updates.append(
            {
                "range": rowcol_to_a1(r_idx, ibod + 1),
                "values": [[nuevo]],
            }
        )

    print("Regla: Barra->BOD-002 | Cocina->BOD-005 | excepcion BOD-001:", ", ".join(sorted(PROV_INGRESO_BOD_001)))
    print(f"Filas a actualizar: {len(updates)}")
    print(f"  -> BOD-005: {stats.get('a_005', 0)}")
    print(f"  -> BOD-002: {stats.get('a_002', 0)}")
    print(f"  -> BOD-001: {stats.get('a_001', 0)}")
    print(f"  Sin cambio: {stats['sin_cambio']}")

    if dry:
        print("[DRY RUN] no escribe en Sheets")
        return 0

    for j in range(0, len(updates), 50):
        batch = updates[j : j + 50]
        for attempt in range(3):
            try:
                ws.batch_update(batch, value_input_option="USER_ENTERED")
                break
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    time.sleep(65 * (attempt + 1))
                    continue
                raise
        print(f"  Escritas {min(j + 50, len(updates))}/{len(updates)}")
        time.sleep(1.2)

    pfd._items_prov_cache = None
    pfd._invalidar_cache_layout_precio_items_prov()
    print("Listo. Cache BD_ITEMS_PROV invalidada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

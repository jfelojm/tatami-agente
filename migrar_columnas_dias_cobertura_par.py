"""
Columna dias_cobertura_par en BD_MP_SISTEMA (política PAR por insumo).

  BD_MP_SISTEMA.dias_cobertura_par  — editar aquí (ej. col morada MP 29 = 3)
  BD_PROV.frecuencia_compra_dias    — fallback si MP vacío; calendario de pedidos
  BD_ITEMS_PROV.prioridad           — solo qué presentación pedir (no PAR)

Opcional: --sembrar-desde-items copia valores legacy de BD_ITEMS_PROV col K al MP.

Uso:
  python migrar_columnas_dias_cobertura_par.py --dry-run
  python migrar_columnas_dias_cobertura_par.py --produccion
  python migrar_columnas_dias_cobertura_par.py --produccion --sembrar-desde-items
"""

from __future__ import annotations

import argparse
import os
import sys

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

from costo_mp_canonico import norm_mp

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _abrir():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _header_row(values: list[list[str]], key_col: str) -> tuple[int, list[str]]:
    for i, row in enumerate(values):
        headers = [(c or "").strip() for c in row]
        if key_col in headers:
            return i, headers
    raise ValueError(f"Sin columna {key_col}")


def _agregar_columnas(
    ws,
    key_col: str,
    nuevas: tuple[str, ...],
    *,
    dry_run: bool,
) -> list[str]:
    vals = ws.get_all_values()
    hi, headers = _header_row(vals, key_col)
    agregadas: list[str] = []
    next_col = len(headers) + 1
    updates = []
    for col in nuevas:
        if col in headers:
            continue
        agregadas.append(col)
        updates.append({"range": rowcol_to_a1(hi + 1, next_col), "values": [[col]]})
        next_col += 1
    if agregadas and not dry_run:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
    return agregadas


def _parse_dias(val: str) -> float | None:
    s = (val or "").strip()
    if not s:
        return None
    try:
        d = float(s.replace(",", "."))
        return d if d > 0 else None
    except ValueError:
        return None


def _sembrar_desde_items(*, dry_run: bool) -> int:
    """Copia dias_cobertura_par de BD_ITEMS_PROV al MP si BD_MP_SISTEMA está vacío."""
    sh = _abrir()
    ws_i = sh.worksheet("BD_ITEMS_PROV")
    vals_i = ws_i.get_all_values()
    hi_i, h_i = _header_row(vals_i, "cod_item_prov")
    if "dias_cobertura_par" not in h_i or "cod_mp_sistema" not in h_i:
        print("  Sembrar: BD_ITEMS_PROV sin dias_cobertura_par o cod_mp_sistema")
        return 0
    ic_mp = h_i.index("cod_mp_sistema")
    ic_d = h_i.index("dias_cobertura_par")
    dias_por_mp: dict[str, float] = {}
    for row in vals_i[hi_i + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        mp = norm_mp(row[ic_mp] if ic_mp < len(row) else "")
        if not mp or mp in dias_por_mp:
            continue
        d = _parse_dias(row[ic_d] if ic_d < len(row) else "")
        if d is not None:
            dias_por_mp[mp] = d
    if not dias_por_mp:
        print("  Sembrar: ningún ítem con dias_cobertura_par")
        return 0

    ws_m = sh.worksheet("BD_MP_SISTEMA")
    vals_m = ws_m.get_all_values()
    hi_m, h_m = _header_row(vals_m, "cod_mp_sistema")
    if "dias_cobertura_par" not in h_m:
        print("  Sembrar: falta columna dias_cobertura_par en BD_MP_SISTEMA")
        return 0
    icod = h_m.index("cod_mp_sistema")
    idias = h_m.index("dias_cobertura_par") + 1
    mp_ya: set[str] = set()
    for row in vals_m[hi_m + 1 :]:
        mp = norm_mp(row[icod] if icod < len(row) else "")
        if not mp:
            continue
        if _parse_dias(row[h_m.index("dias_cobertura_par")] if h_m.index("dias_cobertura_par") < len(row) else ""):
            mp_ya.add(mp)

    updates = []
    escritos = 0
    for i, row in enumerate(vals_m[hi_m + 1 :], start=hi_m + 2):
        mp = norm_mp(row[icod] if icod < len(row) else "")
        if not mp or mp in mp_ya or mp not in dias_por_mp:
            continue
        updates.append({"range": rowcol_to_a1(i, idias), "values": [[dias_por_mp[mp]]]})
        escritos += 1

    print(f"  Sembrar: {len(dias_por_mp)} MPs en ítems | {escritos} celdas MP a rellenar")
    if updates and not dry_run:
        ws_m.batch_update(updates, value_input_option=ValueInputOption.user_entered)
    return escritos


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument(
        "--sembrar-desde-items",
        action="store_true",
        help="Copia dias legacy de BD_ITEMS_PROV a BD_MP_SISTEMA donde esté vacío",
    )
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)
    dry = args.dry_run or not args.produccion
    print(f"migrar_columnas_dias_cobertura_par — {'DRY-RUN' if dry else 'PRODUCCION'}")
    sh = _abrir()
    ws = sh.worksheet("BD_MP_SISTEMA")
    ag = _agregar_columnas(ws, "cod_mp_sistema", ("dias_cobertura_par",), dry_run=dry)
    if ag:
        print(f"  BD_MP_SISTEMA: +{', '.join(ag)}")
    else:
        print("  BD_MP_SISTEMA: columna dias_cobertura_par ya existe")
    if args.sembrar_desde_items:
        _sembrar_desde_items(dry_run=dry)
    print("OK")


if __name__ == "__main__":
    main()

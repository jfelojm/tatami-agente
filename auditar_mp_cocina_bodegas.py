"""
Auditoría: MPs de cocina (BOD-001 + BOD-005) deben existir en ambas bodegas.

Uso:
  python auditar_mp_cocina_bodegas.py
  python auditar_mp_cocina_bodegas.py --csv exports/gap_mp_cocina.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import gspread
from dotenv import load_dotenv

from bodegas_config import normalizar_cod_bodega
from descargo_subreceta import PREFIJO_PSEUDO_MP
from google_credentials import google_credentials
from subrecetas_bodegas_stock import SUBRECETAS_BARRA

load_dotenv(override=True)

BODEGAS_COCINA = ("BOD-001", "BOD-005")
SHEET = "BD_MP_SISTEMA"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _leer_bd_mp(ws) -> tuple[list[str], dict[tuple[str, str], dict]]:
    values = ws.get_all_values()
    hi = next(
        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if hi is None:
        raise RuntimeError("BD_MP_SISTEMA sin header cod_mp_sistema")
    headers = [(c or "").strip() for c in values[hi]]
    filas: dict[tuple[str, str], dict] = {}
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        d = {
            headers[j]: (row[j] if j < len(row) else "").strip()
            for j in range(min(len(headers), len(row)))
            if headers[j]
        }
        cod = (d.get("cod_mp_sistema") or "").strip()
        bod = normalizar_cod_bodega(d.get("cod_bodega"))
        if cod and bod:
            filas[(cod, bod)] = d
    return headers, filas


def _es_mp_barra(cod: str) -> bool:
    cu = cod.upper()
    if cu in SUBRECETAS_BARRA:
        return True
    if cu.startswith(PREFIJO_PSEUDO_MP):
        num = cu.replace(PREFIJO_PSEUDO_MP, "").strip()
        return num in {"051", "052", "053", "054"}
    return False


def _activa(d: dict) -> bool:
    v = (d.get("activa") or "").strip().upper()
    return v not in ("NO", "FALSE", "0", "INACTIVA", "INACTIVO")


def auditar(filas: dict[tuple[str, str], dict]) -> dict:
    en_001 = {cod for (cod, bod) in filas if bod == "BOD-001"}
    en_005 = {cod for (cod, bod) in filas if bod == "BOD-005"}
    en_002 = {cod for (cod, bod) in filas if bod == "BOD-002"}

    cocina_union = (en_001 | en_005) - {c for c in (en_001 | en_005) if _es_mp_barra(c)}
    # MPs solo en barra que no son de cocina — no forzar a cocina
    solo_barra = en_002 - en_001 - en_005

    faltan_005 = sorted(c for c in cocina_union if c in en_001 and c not in en_005)
    faltan_001 = sorted(c for c in cocina_union if c in en_005 and c not in en_001)
    faltan_ambas = sorted(
        c
        for c in cocina_union
        if c not in en_001 and c not in en_005
    )

    inactivas_001 = sorted(
        c for c in en_001 & cocina_union if not _activa(filas.get((c, "BOD-001"), {}))
    )
    inactivas_005 = sorted(
        c for c in en_005 & cocina_union if not _activa(filas.get((c, "BOD-005"), {}))
    )

    return {
        "total_001": len(en_001),
        "total_005": len(en_005),
        "cocina_union": len(cocina_union),
        "en_ambas": len(en_001 & en_005),
        "faltan_005": faltan_005,
        "faltan_001": faltan_001,
        "solo_barra_sin_cocina": len(solo_barra),
        "inactivas_001": inactivas_001,
        "inactivas_005": inactivas_005,
        "filas": filas,
        "cocina_union_set": cocina_union,
    }


def _nombre(filas: dict, cod: str) -> str:
    for bod in BODEGAS_COCINA:
        d = filas.get((cod, bod))
        if d:
            return (d.get("nombre_mp") or cod).strip()
    return cod


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default="", help="Ruta CSV con gaps detallados")
    args = p.parse_args()

    creds = google_credentials(SCOPES)
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet(SHEET)
    _, filas = _leer_bd_mp(ws)
    r = auditar(filas)

    print(f"BD_MP_SISTEMA — auditoría cocina {BODEGAS_COCINA}")
    print(f"  Filas BOD-001: {r['total_001']}")
    print(f"  Filas BOD-005: {r['total_005']}")
    print(f"  Unión cocina (sin batches barra): {r['cocina_union']}")
    print(f"  Ya en ambas bodegas: {r['en_ambas']}")
    print(f"  Faltan en BOD-005 (están en 001): {len(r['faltan_005'])}")
    print(f"  Faltan en BOD-001 (están en 005): {len(r['faltan_001'])}")
    if r["inactivas_001"]:
        print(f"  Inactivas en 001 (revisar): {len(r['inactivas_001'])}")
    if r["inactivas_005"]:
        print(f"  Inactivas en 005 (revisar): {len(r['inactivas_005'])}")

    gaps: list[dict] = []
    for cod in r["faltan_005"]:
        gaps.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": _nombre(filas, cod),
                "falta_en": "BOD-005",
                "existe_en": "BOD-001",
            }
        )
    for cod in r["faltan_001"]:
        gaps.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": _nombre(filas, cod),
                "falta_en": "BOD-001",
                "existe_en": "BOD-005",
            }
        )

    if gaps:
        print("\n  Detalle gaps (primeros 40):")
        for g in gaps[:40]:
            print(f"    {g['cod_mp_sistema']:8} {g['nombre_mp'][:40]:40} falta {g['falta_en']}")
        if len(gaps) > 40:
            print(f"    ... y {len(gaps) - 40} más")
    else:
        print("\n  OK: todas las MPs de cocina están en BOD-001 y BOD-005.")

    csv_path = (args.csv or "").strip()
    if csv_path:
        path = Path(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["cod_mp_sistema", "nombre_mp", "falta_en", "existe_en"])
            w.writeheader()
            w.writerows(gaps)
        print(f"\n  CSV: {path}")


if __name__ == "__main__":
    main()

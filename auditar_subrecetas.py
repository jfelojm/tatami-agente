"""
Auditoría de BD_SUBRECETAS y BD_SUBRECETAS_DETALLE.

  python auditar_subrecetas.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas,
    cargar_bd_subrecetas_detalle,
    es_linea_mp_detalle,
    es_linea_subreceta_hijo,
    orden_produccion,
)

load_dotenv(override=True)


def _cargar_mps() -> set[str]:
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    values = sh.worksheet("BD_MP_SISTEMA").get_all_values()
    hi = next(i for i, r in enumerate(values) if "cod_mp_sistema" in r)
    ic = [x.strip() for x in values[hi]].index("cod_mp_sistema")
    return {(row[ic] if ic < len(row) else "").strip() for row in values[hi + 1 :] if ic < len(row) and row[ic].strip()}


def auditar() -> int:
    cab = cargar_bd_subrecetas()
    lineas = cargar_bd_subrecetas_detalle()
    por_padre = agrupar_detalle_por_padre(lineas)
    mps = _cargar_mps()
    issues: list[str] = []

    for c, r in cab.items():
        if (r.get("activa") or "").strip().upper() == "SI" and c not in por_padre:
            issues.append(f"Padre {c} ({r.get('nombre_subreceta')}) activo sin detalle")

    for i, row in enumerate(lineas, start=2):
        padre = (row.get("cod_subreceta_padre") or "").strip()
        hijo = (row.get("cod_subreceta_hijo") or "").strip()
        cmp_ = (row.get("cod_mp_sistema") or "").strip()
        qty = (row.get("cantidad") or "").strip()
        uni = (row.get("unidad_base") or "").strip()
        bod = (row.get("cod_bodega") or "").strip()

        if padre not in cab:
            issues.append(f"Fila ~{i}: padre {padre} no en cabecera")
        if hijo and cmp_:
            issues.append(f"Fila ~{i}: padre {padre} tiene hijo {hijo} y MP {cmp_}")
        if not hijo and not cmp_:
            issues.append(f"Fila ~{i}: padre {padre} sin hijo ni MP")
        if hijo and hijo not in cab:
            issues.append(f"Fila ~{i}: hijo {hijo} no en cabecera")
        if hijo == padre:
            issues.append(f"Fila ~{i}: hijo igual al padre {padre}")
        if cmp_ and cmp_ not in mps:
            issues.append(f"Fila ~{i}: MP {cmp_} no en BD_MP_SISTEMA")
        if not qty:
            issues.append(f"Fila ~{i}: cantidad vacía (padre {padre})")
        if (hijo or cmp_) and not uni:
            issues.append(f"Fila ~{i}: unidad_base vacía (padre {padre})")
        if not bod:
            issues.append(f"Fila ~{i}: cod_bodega vacío (padre {padre})")

    anidadas = [r for r in lineas if es_linea_subreceta_hijo(r)]
    print(f"Subrecetas cabecera: {len(cab)}")
    print(f"Lineas detalle: {len(lineas)}")
    print(f"Padres con detalle: {len(por_padre)}")
    print(f"Lineas subreceta hijo: {len(anidadas)}")
    for r in anidadas:
        p = r.get("cod_subreceta_padre", "")
        h = r.get("cod_subreceta_hijo", "")
        print(f"  {p} -> {h}: {r.get('cantidad')} {r.get('unidad_base')}")

    try:
        ord_ = orden_produccion(cab, por_padre)
        print(f"\nOrden produccion (hijos antes): {', '.join(ord_[:15])}...")
        # padres con hijos al final del snippet
        con_hijo = [c for c in ord_ if any(es_linea_subreceta_hijo(r) for r in por_padre.get(c, []))]
        print(f"Con subreceta hijo ({len(con_hijo)}): {', '.join(con_hijo)}")
    except ValueError as e:
        issues.append(str(e))

    print(f"\nIssues: {len(issues)}")
    for x in issues:
        print(f"  - {x}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(auditar())

"""
Utilidades para BD_RECETAS_DETALLE (MP vs subreceta) y agrupación por plato.
"""

from __future__ import annotations

import os
from collections import defaultdict

import gspread
from dotenv import load_dotenv
from google_credentials import google_credentials, pin_cloud_env, spreadsheet_id

load_dotenv(override=False)

SHEET_DETALLE = "BD_RECETAS_DETALLE"
SHEET_RESUMEN = "BD_RECETAS"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Orden de columnas en el maestro (fila 1)
HEADERS_BD_RECETAS_DETALLE_BASE = [
    "nombre_receta",
    "cod_receta",
    "variedad_smart_menu",
    "nombre_subreceta",
    "cod_subreceta",
    "nombre_mp",
    "cod_mp_sistema",
    "cantidad",
    "unidad_base",
    "cod_bodega",
    "merma_pct",
    "es_opcional",
    "pct_aplicacion",
]

# Calculadas por calcular_costo_recetas.py (vacías al promover desde staging)
COLS_COSTO_RECETA_DETALLE = [
    "costo_unitario",
    "costo_linea",
    "nota_costo",
]

HEADERS_BD_RECETAS_DETALLE = HEADERS_BD_RECETAS_DETALLE_BASE + COLS_COSTO_RECETA_DETALLE


def es_linea_subreceta(row: dict) -> bool:
    return bool((row.get("cod_subreceta") or "").strip())


def es_linea_mp(row: dict) -> bool:
    cod_mp = (row.get("cod_mp_sistema") or "").strip()
    if not cod_mp or cod_mp.startswith("#"):
        return False
    return not es_linea_subreceta(row)


def filtrar_solo_mp(lineas: list[dict]) -> list[dict]:
    return [r for r in lineas if es_linea_mp(r)]


def filtrar_solo_subreceta(lineas: list[dict]) -> list[dict]:
    return [r for r in lineas if es_linea_subreceta(r)]


def _limpiar_variedad(variedad: str | None) -> str:
    s = (variedad or "").strip().upper()
    for ch in ("\u00a0", "\u2007", "\u2009", "\u202f", "\ufeff"):
        s = s.replace(ch, " ")
    if "OBS:" in s:
        s = s.split("OBS:", 1)[0].strip()
    return " ".join(s.split())


def norm_cod_receta(cod: str) -> str:
    s = (cod or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return str(int(s))
    return s


def clave_plato(cod_receta: str, variedad_smart_menu: str | None = None) -> str:
    """Clave única plato = cod_receta + variedad (vacía = base)."""
    return f"{norm_cod_receta(cod_receta)}|{_limpiar_variedad(variedad_smart_menu)}"


def _abrir_maestro():
    pin_cloud_env()
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(spreadsheet_id())


def _fila_dict(headers: list[str], row: list) -> dict:
    return {
        headers[k]: (row[k] if k < len(row) else "").strip()
        for k in range(min(len(headers), len(row)))
        if headers[k]
    }


def cargar_bd_recetas_detalle(sh=None) -> list[dict]:
    """Todas las líneas ingrediente (MP o subreceta)."""
    sh = sh or _abrir_maestro()
    values = sh.worksheet(SHEET_DETALLE).get_all_values()
    hi = next(
        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_receta" for c in r)),
        None,
    )
    if hi is None:
        return []
    headers = [(c or "").strip() for c in values[hi]]
    out: list[dict] = []
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        r = _fila_dict(headers, row)
        if not (r.get("cod_receta") or "").strip():
            continue
        if not es_linea_mp(r) and not es_linea_subreceta(r):
            continue
        out.append(r)
    return out


def agrupar_por_plato(lineas: list[dict]) -> dict[str, list[dict]]:
    """clave_plato -> líneas de BD_RECETAS_DETALLE."""
    por: dict[str, list[dict]] = defaultdict(list)
    for ln in lineas:
        cod = (ln.get("cod_receta") or "").strip()
        var = ln.get("variedad_smart_menu", "")
        por[clave_plato(cod, var)].append(ln)
    return dict(por)

"""
Utilidades para BD_SUBRECETAS y BD_SUBRECETAS_DETALLE (MP vs subreceta hijo).

Detalle (por lote estándar del padre):
  cod_subreceta_padre, cod_subreceta_hijo, cod_mp_sistema, cantidad, unidad_base, cod_bodega
  — exactamente uno de cod_subreceta_hijo o cod_mp_sistema por fila.
"""

from __future__ import annotations

import os
from collections import defaultdict

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv(override=True)

SHEET_CABECERA = "BD_SUBRECETAS"
SHEET_DETALLE = "BD_SUBRECETAS_DETALLE"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def es_linea_mp_detalle(row: dict) -> bool:
    cod_mp = (row.get("cod_mp_sistema") or "").strip()
    cod_hijo = (row.get("cod_subreceta_hijo") or "").strip()
    if not cod_mp or cod_mp.startswith("#"):
        return False
    return not cod_hijo


def es_linea_subreceta_hijo(row: dict) -> bool:
    return bool((row.get("cod_subreceta_hijo") or "").strip()) and not es_linea_mp_detalle(row)


def filtrar_lineas_mp(lineas: list[dict]) -> list[dict]:
    return [r for r in lineas if es_linea_mp_detalle(r)]


def filtrar_lineas_subreceta_hijo(lineas: list[dict]) -> list[dict]:
    return [r for r in lineas if es_linea_subreceta_hijo(r)]


def _abrir_maestro():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _fila_a_dict(headers: list[str], row: list) -> dict:
    """Primera aparición de cada nombre de columna gana (evita duplicados en fila 1)."""
    out: dict = {}
    for i, h in enumerate(headers):
        key = (h or "").strip()
        if not key or key in out:
            continue
        out[key] = row[i].strip() if i < len(row) else ""
    # alias legado
    if "cod_subreceta_padre" in out and "cod_subreceta" not in out:
        out["cod_subreceta"] = out["cod_subreceta_padre"]
    return out


def _buscar_header(values: list[list], columnas_minimas: tuple[str, ...]) -> tuple[int, list[str]]:
    for i, row in enumerate(values):
        headers = [(c or "").strip() for c in row]
        if all(any(h == col for h in headers) for col in columnas_minimas):
            return i, headers
    raise ValueError(f"No se encontró cabecera con columnas {columnas_minimas}")


def cargar_bd_subrecetas(sh=None) -> dict[str, dict]:
    """cod_subreceta -> cabecera."""
    sh = sh or _abrir_maestro()
    values = sh.worksheet(SHEET_CABECERA).get_all_values()
    hi, headers = _buscar_header(values, ("cod_subreceta", "rendimiento_estandar"))
    out: dict[str, dict] = {}
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        r = _fila_a_dict(headers, row)
        cod = (r.get("cod_subreceta") or "").strip()
        if not cod:
            continue
        out[cod] = r
    return out


def cargar_bd_subrecetas_detalle(sh=None) -> list[dict]:
    sh = sh or _abrir_maestro()
    values = sh.worksheet(SHEET_DETALLE).get_all_values()
    hi, headers = _buscar_header(
        values, ("cod_subreceta_padre", "cod_mp_sistema", "cantidad")
    )
    lineas: list[dict] = []
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row[:8]):
            continue
        lineas.append(_fila_a_dict(headers, row))
    return lineas


def agrupar_detalle_por_padre(lineas: list[dict]) -> dict[str, list[dict]]:
    por: dict[str, list[dict]] = defaultdict(list)
    for r in lineas:
        padre = (r.get("cod_subreceta_padre") or r.get("cod_subreceta") or "").strip()
        if padre:
            por[padre].append(r)
    return dict(por)


def hijos_subreceta_de(padre: str, lineas: list[dict]) -> list[str]:
    """Códigos de subrecetas hijas usadas en el detalle del padre."""
    p = padre.strip()
    return [
        (r.get("cod_subreceta_hijo") or "").strip()
        for r in lineas
        if (r.get("cod_subreceta_padre") or r.get("cod_subreceta") or "").strip() == p
        and (r.get("cod_subreceta_hijo") or "").strip()
    ]


def orden_produccion(cabeceras: dict[str, dict], por_padre: dict[str, list[dict]]) -> list[str]:
    """
    Orden topológico: producir hijos antes que padres (solo subrecetas activas con detalle).
    """
    activas = {
        c
        for c, r in cabeceras.items()
        if (r.get("activa") or "SI").strip().upper() == "SI" and c in por_padre
    }
    deps: dict[str, set[str]] = {}
    for p in activas:
        deps[p] = {h for h in hijos_subreceta_de(p, por_padre[p]) if h in activas}

    orden: list[str] = []
    restantes = set(activas)

    while restantes:
        listos = {n for n in restantes if not (deps[n] & restantes)}
        if not listos:
            raise ValueError(f"Ciclo o dependencia inválida entre subrecetas: {restantes}")
        for n in sorted(listos):
            orden.append(n)
            restantes.remove(n)
    return orden


def factor_lote(cantidad_producida: float, rendimiento_estandar: float) -> float:
    if rendimiento_estandar <= 0:
        return 0.0
    return cantidad_producida / rendimiento_estandar


def cantidades_escaladas(lineas: list[dict], factor: float) -> list[dict]:
    """Copia líneas con cantidad escalada por factor de lote."""
    out: list[dict] = []
    for r in lineas:
        try:
            from numeros_sheets import parse_numero_sheets

            base = parse_numero_sheets(r.get("cantidad", "0"))
        except ValueError:
            base = 0.0
        copia = dict(r)
        copia["cantidad_escalada"] = round(base * factor, 6)
        out.append(copia)
    return out

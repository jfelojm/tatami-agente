"""
Días de cobertura para PAR (multiplicador del consumo diario).

Cascada:
  1. BD_MP_SISTEMA.dias_cobertura_par (por cod_mp, manual — una política por insumo)
  2. BD_PROV.frecuencia_compra_dias vía ítem preferido (solo si MP vacío; transición)
  3. BD_CONFIG.par_level_dias_cobertura (7)

Subrecetas SUB-*: siempre BD_CONFIG.

prioridad en BD_ITEMS_PROV: solo para elegir presentación al pedir, no para PAR.
"""

from __future__ import annotations

import os
from functools import lru_cache

from config_sheets import cfg
from costo_mp_canonico import norm_mp
from descargo_subreceta import PREFIJO_PSEUDO_MP

_COL_FRECUENCIA_PROV = ("frecuencia_compra_dias", "frecuencia_entrega_dias")


def _norm_cod_prov(cod: str) -> str:
    s = (cod or "").strip()
    if s.isdigit():
        return str(int(s)).zfill(3)
    return s


def es_pseudo_mp_subreceta(cod_mp: str) -> bool:
    return (cod_mp or "").strip().upper().startswith(PREFIJO_PSEUDO_MP)


def dias_cobertura_global_default() -> float:
    raw = cfg("par_level_dias_cobertura", os.getenv("PAR_LEVEL_DIAS_COBERTURA", "7") or "7")
    try:
        d = float(str(raw).replace(",", "."))
        return d if d > 0 else 7.0
    except (TypeError, ValueError):
        return 7.0


def _parse_dias_positivos(val: object) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.startswith("#"):
        return None
    try:
        d = float(s.replace(",", "."))
        return d if d > 0 else None
    except ValueError:
        return None


def _parse_prioridad(val: object) -> int:
    if val is None:
        return 99
    s = str(val).strip()
    if not s:
        return 99
    try:
        return int(float(s.replace(",", ".")))
    except ValueError:
        return 99


def _columna_frecuencia_prov(headers: list[str]) -> str | None:
    for name in _COL_FRECUENCIA_PROV:
        if name in headers:
            return name
    return None


def _abrir_maestro():
    import gspread
    from dotenv import load_dotenv
    from google.oauth2.service_account import Credentials

    load_dotenv(override=True)
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


@lru_cache(maxsize=1)
def _cargar_dias_mp_sistema() -> dict[str, float]:
    """cod_mp_norm -> dias_cobertura_par (primera fila no vacía en BD_MP_SISTEMA)."""
    sh = _abrir_maestro()
    vals = sh.worksheet("BD_MP_SISTEMA").get_all_values()
    hi = next(
        (i for i, r in enumerate(vals) if any((c or "").strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if hi is None:
        return {}
    headers = [(c or "").strip() for c in vals[hi]]
    if "dias_cobertura_par" not in headers:
        return {}
    icod = headers.index("cod_mp_sistema")
    idias = headers.index("dias_cobertura_par")
    out: dict[str, float] = {}
    for row in vals[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        cod = norm_mp(row[icod] if icod < len(row) else "")
        if not cod or cod in out:
            continue
        dias = _parse_dias_positivos(row[idias] if idias < len(row) else "")
        if dias is not None:
            out[cod] = dias
    return out


@lru_cache(maxsize=1)
def _cargar_frecuencia_compra_prov() -> dict[str, float]:
    """cod_proveedor -> frecuencia_compra_dias."""
    sh = _abrir_maestro()
    vals = sh.worksheet("BD_PROV").get_all_values()
    hi = next(
        (i for i, r in enumerate(vals) if any((c or "").strip() == "cod_proveedor" for c in r)),
        None,
    )
    if hi is None:
        return {}
    headers = [(c or "").strip() for c in vals[hi]]
    col_freq = _columna_frecuencia_prov(headers)
    if not col_freq:
        return {}
    icod = headers.index("cod_proveedor")
    ifreq = headers.index(col_freq)
    out: dict[str, float] = {}
    for row in vals[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        cod = _norm_cod_prov(row[icod] if icod < len(row) else "")
        if not cod or cod.startswith("["):
            continue
        dias = _parse_dias_positivos(row[ifreq] if ifreq < len(row) else "")
        if dias is not None:
            out[cod] = dias
    return out


@lru_cache(maxsize=1)
def _frecuencia_proveedor_preferido_por_mp() -> dict[str, float]:
    """Fallback transitorio: frecuencia del proveedor del ítem preferido (prioridad)."""
    from procesar_facturas_drive import cargar_bd_items_prov

    freq_prov = _cargar_frecuencia_compra_prov()
    por_mp: dict[str, list[tuple[int, str]]] = {}
    for it in cargar_bd_items_prov():
        cod_mp = norm_mp(it.get("cod_mp_sistema") or "")
        if not cod_mp:
            continue
        cp = _norm_cod_prov(it.get("cod_proveedor") or "")
        por_mp.setdefault(cod_mp, []).append((_parse_prioridad(it.get("prioridad")), cp))
    out: dict[str, float] = {}
    for cod_mp, rows in por_mp.items():
        rows.sort(key=lambda x: (x[0], x[1]))
        for _, cp in rows:
            f = freq_prov.get(cp)
            if f is not None:
                out[cod_mp] = f
                break
    return out


def invalidar_cache_dias_cobertura() -> None:
    _cargar_dias_mp_sistema.cache_clear()
    _cargar_frecuencia_compra_prov.cache_clear()
    _frecuencia_proveedor_preferido_por_mp.cache_clear()


def resolver_dias_cobertura_mp(cod_mp: str) -> tuple[float, str]:
    """
    Devuelve (días, fuente).
    fuente: subreceta_config | mp_sistema | frecuencia_compra | config
    """
    nk = norm_mp(cod_mp)
    if not nk:
        return dias_cobertura_global_default(), "config"

    if es_pseudo_mp_subreceta(nk):
        return dias_cobertura_global_default(), "subreceta_config"

    mp_dias = _cargar_dias_mp_sistema().get(nk)
    if mp_dias is not None:
        return mp_dias, "mp_sistema"

    freq = _frecuencia_proveedor_preferido_por_mp().get(nk)
    if freq is not None:
        return freq, "frecuencia_compra"

    return dias_cobertura_global_default(), "config"


def mapa_dias_cobertura_por_mp(cod_mps: list[str] | None = None) -> dict[str, float]:
    if cod_mps is None:
        mp_d = set(_cargar_dias_mp_sistema())
        mp_d.update(_frecuencia_proveedor_preferido_por_mp())
        cod_mps = list(mp_d)
    return {norm_mp(c): resolver_dias_cobertura_mp(c)[0] for c in cod_mps if norm_mp(c)}


def mapa_dias_cobertura_con_fuente(cod_mps: list[str]) -> dict[str, tuple[float, str]]:
    return {norm_mp(c): resolver_dias_cobertura_mp(c) for c in cod_mps if norm_mp(c)}

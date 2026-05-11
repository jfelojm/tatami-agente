import os
from functools import lru_cache
from typing import Any

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials


load_dotenv(override=True)


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _sheet():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _coerce(value: str, default: Any = None) -> Any:
    if value is None:
        return default
    s = str(value).strip()
    if s == "":
        return default

    low = s.lower()
    if low in {"true", "si", "sí", "1", "yes", "y"}:
        return True
    if low in {"false", "no", "0", "n"}:
        return False

    try:
        if "." in s or "," in s:
            return float(s.replace(",", "."))
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
    except Exception:
        pass
    return s


@lru_cache(maxsize=8)
def _cargar_hoja_clave_valor(titulo_hoja: str) -> dict[str, Any]:
    """
    Lee una pestaña con columnas clave / valor (mismo formato que BD_CONFIG).
    Si la hoja no existe, devuelve {}.
    """
    try:
        ws = _sheet().worksheet(titulo_hoja)
    except Exception:
        return {}

    values = ws.get_all_values()
    if not values:
        return {}

    headers = [h.strip().lower() for h in values[0]]
    try:
        i_clave = headers.index("clave")
        i_valor = headers.index("valor")
    except ValueError:
        return {}

    out: dict[str, Any] = {}
    for row in values[1:]:
        if len(row) <= max(i_clave, i_valor):
            continue
        clave = (row[i_clave] or "").strip()
        if not clave or clave.startswith("#"):
            continue
        raw = row[i_valor] if i_valor < len(row) else ""
        out[clave] = _coerce(raw, default=None)
    return out


@lru_cache(maxsize=1)
def cargar_bd_config() -> dict[str, Any]:
    """
    Lee hoja BD_CONFIG (clave/valor/descripcion) y devuelve dict clave->valor (coerce).
    Si la hoja no existe, devuelve {}.
    """
    return _cargar_hoja_clave_valor("BD_CONFIG")


def cargar_parametros_sheet() -> dict[str, Any]:
    """
    Hoja PARAMETROS (mismo esquema clave/valor que BD_CONFIG), si existe.
    Pensada para parámetros operativos; si no hay pestaña, {}.
    """
    return _cargar_hoja_clave_valor("PARAMETROS")


def cfg(key: str, default: Any = None) -> Any:
    return cargar_bd_config().get(key, default)


def cfg_tokens(key: str, default: set[str] | None = None) -> set[str]:
    """
    Lee un config tipo "A,B,C" y lo devuelve como set upper.
    """
    default = default or set()
    v = cfg(key, None)
    if v is None:
        return set(default)
    if isinstance(v, (list, tuple, set)):
        return {str(x).strip().upper() for x in v if str(x).strip()}
    s = str(v)
    parts = [p.strip().upper() for p in s.replace(";", ",").split(",")]
    return {p for p in parts if p}


def delta_abs_tol_conteo(cli_override: float | None = None) -> float:
    """
    Umbral |delta| por debajo del cual conteo_fisico no inserta mov_inventario (ruido de redondeo).

    Precedencia: --tol CLI > CONTEO_DELTA_ABS_TOL > PARAMETROS.conteo_delta_abs_tol >
                 BD_CONFIG.conteo_delta_abs_tol > 0.001
    """
    default = 0.001
    if cli_override is not None:
        return max(0.0, float(cli_override))

    env = os.getenv("CONTEO_DELTA_ABS_TOL")
    if env and str(env).strip():
        try:
            return max(0.0, float(str(env).replace(",", ".")))
        except ValueError:
            pass

    for fuente in (cargar_parametros_sheet(), cargar_bd_config()):
        v = fuente.get("conteo_delta_abs_tol")
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return max(0.0, float(v))
        s = str(v).strip()
        if not s:
            continue
        try:
            return max(0.0, float(s.replace(",", ".")))
        except ValueError:
            continue

    return default

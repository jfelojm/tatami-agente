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


@lru_cache(maxsize=1)
def cargar_bd_config() -> dict[str, Any]:
    """
    Lee hoja BD_CONFIG (clave/valor/descripcion) y devuelve dict clave->valor (coerce).
    Si la hoja no existe, devuelve {}.
    """
    try:
        ws = _sheet().worksheet("BD_CONFIG")
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


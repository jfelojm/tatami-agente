"""Carga credenciales Google: archivo local o JSON en variable de entorno (nube)."""
from __future__ import annotations

import json
import os

from google.oauth2.service_account import Credentials

# Captura temprana (Railway) antes de load_dotenv(override=True) en submódulos.
_PINNED_ENV: dict[str, str] = {
    "GOOGLE_CREDENTIALS_JSON": (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip(),
    "SPREADSHEET_ID": (os.getenv("SPREADSHEET_ID") or "").strip(),
}


def pin_cloud_env() -> None:
    """Restaura vars de nube si load_dotenv las borró o dejó vacías."""
    for key, val in _PINNED_ENV.items():
        if val and not (os.getenv(key) or "").strip():
            os.environ[key] = val


def has_google_credentials() -> bool:
    """True si hay JSON en env o ruta a archivo local."""
    pin_cloud_env()
    if (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip():
        return True
    for key in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS_PATH"):
        path = (os.getenv(key) or "").strip()
        if path and os.path.isfile(path):
            return True
    return False


def spreadsheet_id() -> str:
    """ID del maestro Google Sheets."""
    pin_cloud_env()
    sid = (os.getenv("SPREADSHEET_ID") or "").strip()
    if not sid:
        raise RuntimeError(
            "Falta SPREADSHEET_ID. Configúralo en Railway Variables o .env local."
        )
    return sid


def _credentials_from_json(raw_json: str, scopes: list[str]) -> Credentials:
    try:
        info = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON no es JSON válido. "
            "Pega el service account completo en Railway (una sola línea)."
        ) from e
    if not isinstance(info, dict):
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON debe ser un objeto JSON.")
    return Credentials.from_service_account_info(info, scopes=scopes)


def google_credentials(scopes: list[str]) -> Credentials:
    """
    Orden:
    1. GOOGLE_CREDENTIALS_JSON — texto JSON (Railway/Render)
    2. GOOGLE_APPLICATION_CREDENTIALS — ruta estándar Google
    3. GOOGLE_CREDENTIALS_PATH — ruta local legacy
    """
    pin_cloud_env()

    raw_json = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
    if raw_json:
        return _credentials_from_json(raw_json, scopes)

    for key in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS_PATH"):
        path = (os.getenv(key) or "").strip()
        if path and os.path.isfile(path):
            return Credentials.from_service_account_file(path, scopes=scopes)

    raise RuntimeError(
        "Faltan credenciales Google en el servidor. "
        "En Railway define GOOGLE_CREDENTIALS_JSON con el JSON del service account "
        "(no uses GOOGLE_CREDENTIALS_PATH: la carpeta credentials/ no va en Docker)."
    )


def open_gspread_workbook(scopes: list[str] | None = None):
    """Abre el spreadsheet maestro (compartido por webhook y producción)."""
    import gspread

    sc = scopes or ["https://www.googleapis.com/auth/spreadsheets"]
    creds = google_credentials(sc)
    return gspread.authorize(creds).open_by_key(spreadsheet_id())

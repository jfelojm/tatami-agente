"""Carga credenciales Google: archivo local o JSON en variable de entorno (nube)."""
from __future__ import annotations

import json
import os

from google.oauth2.service_account import Credentials


def has_google_credentials() -> bool:
    """True si hay JSON en env o ruta a archivo local."""
    if (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip():
        return True
    path = (os.getenv("GOOGLE_CREDENTIALS_PATH") or "").strip()
    return bool(path and os.path.isfile(path))


def google_credentials(scopes: list[str]) -> Credentials:
    """
    Orden:
    1. GOOGLE_CREDENTIALS_JSON — texto JSON del service account (Railway/Render)
    2. GOOGLE_CREDENTIALS_PATH — ruta al .json (PC / servidor local)
    """
    raw_json = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
    if raw_json:
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON no es JSON valido. "
                "Pegue el contenido completo del service account en Railway."
            ) from e
        return Credentials.from_service_account_info(info, scopes=scopes)

    path = (os.getenv("GOOGLE_CREDENTIALS_PATH") or "").strip()
    if path and os.path.isfile(path):
        return Credentials.from_service_account_file(path, scopes=scopes)

    raise RuntimeError(
        "Faltan credenciales Google: defina GOOGLE_CREDENTIALS_JSON (nube) "
        "o GOOGLE_CREDENTIALS_PATH (local)."
    )

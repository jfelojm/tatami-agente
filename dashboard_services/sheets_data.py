"""Lectura de hojas Google Sheets para dashboards (sin importar whatsapp_webhook)."""

from __future__ import annotations

import os
import time

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_cache: dict[str, tuple[float, list[dict]]] = {}
_TTL = 120.0


def _conectar():
    path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/google_service_account.json")
    sid = os.getenv("SPREADSHEET_ID", "")
    creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(sid)


def leer_hoja(sheet_name: str, header_key: str, *, skip: int = 1) -> list[dict]:
    now = time.monotonic()
    hit = _cache.get(sheet_name)
    if hit and (now - hit[0]) < _TTL:
        return list(hit[1])

    sh = _conectar()
    ws = sh.worksheet(sheet_name)
    values = ws.get_all_values()
    header_row = None
    for i, row in enumerate(values):
        if any((c or "").strip() == header_key for c in row):
            header_row = i
            break
    if header_row is None:
        return []
    headers = [(c or "").strip() for c in values[header_row]]
    out: list[dict] = []
    for row in values[header_row + skip :]:
        if not any((c or "").strip() for c in row):
            continue
        if row and str(row[0]).strip().startswith("["):
            continue
        d = {
            headers[j]: (row[j] if j < len(row) else "").strip()
            for j in range(len(headers))
            if headers[j]
        }
        out.append(d)
    _cache[sheet_name] = (now, out)
    return list(out)


def leer_bd_mp_sistema() -> list[dict]:
    return leer_hoja("BD_MP_SISTEMA", "cod_mp_sistema")


def leer_bd_prov() -> list[dict]:
    return leer_hoja("BD_PROV", "cod_proveedor")


def leer_bd_recetas() -> list[dict]:
    return leer_hoja("BD_RECETAS", "cod_receta")

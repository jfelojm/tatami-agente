"""Lectura de hojas Google Sheets para dashboards (sin importar whatsapp_webhook)."""

from __future__ import annotations

import os
import time

import gspread
from google_credentials import google_credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_cache: dict[str, tuple[float, list[dict]]] = {}
_TTL = 120.0
_MAX_RETRIES = 3
_RETRY_SLEEP_SEC = 2.0


def _conectar():
    from dotenv import load_dotenv
    from google_credentials import pin_cloud_env, spreadsheet_id

    load_dotenv(override=False)
    pin_cloud_env()
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(spreadsheet_id())


def _fmt_sheets_error(sheet_name: str, exc: BaseException) -> str:
    msg = str(exc).strip()
    if not msg:
        msg = repr(exc).strip("'")
    return f"{sheet_name}: {type(exc).__name__}: {msg}"


def leer_hoja(sheet_name: str, header_key: str, *, skip: int = 1) -> list[dict]:
    now = time.monotonic()
    hit = _cache.get(sheet_name)
    if hit and (now - hit[0]) < _TTL:
        return list(hit[1])

    last_err: BaseException | None = None
    values: list[list[str]] | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            sh = _conectar()
            ws = sh.worksheet(sheet_name)
            values = ws.get_all_values()
            break
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_SLEEP_SEC * attempt)
    if values is None:
        raise RuntimeError(_fmt_sheets_error(sheet_name, last_err or RuntimeError("sin detalle")))
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

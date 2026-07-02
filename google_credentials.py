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


def _running_on_railway() -> bool:
    return bool(
        (os.getenv("RAILWAY_ENVIRONMENT") or "").strip()
        or (os.getenv("RAILWAY_PROJECT_ID") or "").strip()
        or (os.getenv("RAILWAY_SERVICE_ID") or "").strip()
    )


def pin_cloud_env() -> None:
    """Restaura vars de nube si load_dotenv las borró o dejó vacías."""
    for key in _PINNED_ENV:
        cur = (os.getenv(key) or "").strip()
        if cur and len(cur) >= len(_PINNED_ENV.get(key) or ""):
            _PINNED_ENV[key] = cur
    for key, val in _PINNED_ENV.items():
        if val and not (os.getenv(key) or "").strip():
            os.environ[key] = val
    if _running_on_railway() and (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip():
        for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS_PATH"):
            os.environ.pop(k, None)


def _normalize_raw_json(raw: str) -> str:
    """Limpia JSON pegado en Railway (comillas externas, private_key roto)."""
    s = (raw or "").strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        try:
            s = json.loads(s)
            if isinstance(s, str):
                s = s.strip()
            elif isinstance(s, dict):
                return json.dumps(s)
        except json.JSONDecodeError:
            s = s[1:-1].strip()
    if s.startswith("{") and "\\n" in s and "\n" not in s.split("private_key", 1)[-1][:80]:
        pass
    try:
        info = json.loads(s)
    except json.JSONDecodeError:
        return s
    if not isinstance(info, dict):
        return s
    pk = info.get("private_key")
    if isinstance(pk, str) and "\\n" in pk and "\n" not in pk:
        info["private_key"] = pk.replace("\\n", "\n")
    return json.dumps(info)


def google_credentials_status() -> dict:
    """Diagnóstico seguro para / health (sin secretos)."""
    pin_cloud_env()
    raw = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
    path_vars = {
        k: (os.getenv(k) or "").strip()
        for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS_PATH")
    }
    out: dict = {
        "json_len": len(raw),
        "json_parse_ok": False,
        "client_email": None,
        "private_key_id": None,
        "path_vars": {k: v for k, v in path_vars.items() if v},
        "path_is_file": {
            k: os.path.isfile(v) for k, v in path_vars.items() if v
        },
        "railway": _running_on_railway(),
        "pinned_json_len": len(_PINNED_ENV.get("GOOGLE_CREDENTIALS_JSON") or ""),
    }
    if not raw:
        out["json_error"] = "GOOGLE_CREDENTIALS_JSON vacío"
        return out
    try:
        norm = _normalize_raw_json(raw)
        info = json.loads(norm)
        if not isinstance(info, dict):
            out["json_error"] = "JSON no es objeto"
            return out
        out["json_parse_ok"] = True
        out["client_email"] = info.get("client_email")
        out["private_key_id"] = info.get("private_key_id")
        pk = info.get("private_key") or ""
        if isinstance(pk, str):
            out["private_key_lines"] = pk.count("\n") + (1 if pk else 0)
    except Exception as e:
        out["json_error"] = f"{type(e).__name__}: {e}"
    return out


def has_google_credentials() -> bool:
    """True si hay JSON en env o ruta a archivo local."""
    pin_cloud_env()
    if (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip():
        return True
    if _running_on_railway():
        return False
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
    norm = _normalize_raw_json(raw_json)
    try:
        info = json.loads(norm)
    except json.JSONDecodeError as e:
        preview = norm[:80].replace("\n", " ")
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON no es JSON válido. "
            f"Inicio: {preview!r}… Pega el service account completo en Railway (una sola línea)."
        ) from e
    if not isinstance(info, dict):
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON debe ser un objeto JSON.")
    pk = info.get("private_key")
    if isinstance(pk, str):
        if "\\n" in pk and "\n" not in pk:
            info["private_key"] = pk.replace("\\n", "\n")
        if "BEGIN PRIVATE KEY" not in pk:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON: private_key inválido o truncado al pegar en Railway."
            )
    return Credentials.from_service_account_info(info, scopes=scopes)


def google_credentials(scopes: list[str]) -> Credentials:
    """
    Orden:
    1. GOOGLE_CREDENTIALS_JSON — texto JSON (Railway/Render)
    2. GOOGLE_APPLICATION_CREDENTIALS — ruta estándar Google (solo local)
    3. GOOGLE_CREDENTIALS_PATH — ruta local legacy
    """
    pin_cloud_env()

    raw_json = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
    if raw_json:
        return _credentials_from_json(raw_json, scopes)

    if _running_on_railway():
        st = google_credentials_status()
        hint = st.get("json_error") or "variable vacía o mal pegada"
        raise RuntimeError(
            f"Falta GOOGLE_CREDENTIALS_JSON válido en Railway ({hint}). "
            "Usa deploy/copiar_google_json_railway.ps1 y elimina GOOGLE_CREDENTIALS_PATH."
        )

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

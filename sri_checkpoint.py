"""Estado de la última corrida SRI (portal + descarga + proceso)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ZONA_EC = ZoneInfo("America/Guayaquil")
CHECKPOINT_PATH = Path(__file__).resolve().parent / "logs" / "sri_checkpoint.json"


def leer_checkpoint() -> dict[str, Any] | None:
    if not CHECKPOINT_PATH.is_file():
        return None
    try:
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def escribir_checkpoint(data: dict[str, Any]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZONA_EC)
        return dt.astimezone(ZONA_EC).strftime("%d/%m %H:%M")
    except Exception:
        return iso[:16]


def resumen_ultima_ok(prev: dict[str, Any] | None) -> str:
    """Texto corto para WA cuando la corrida actual falla o viene tras caída."""
    if not prev:
        return "Sin corrida SRI registrada antes."
    if (prev.get("status") or "").upper() not in ("OK", "PARTIAL"):
        prev_ok = prev.get("last_ok") or prev
        if prev_ok and (prev_ok.get("status") or "").upper() in ("OK", "PARTIAL"):
            prev = prev_ok
        else:
            return (
                f"Última corrida registrada: {prev.get('status', '?')} "
                f"({prev.get('corrida', '?')} {_fmt_ts(prev.get('updated'))})"
            )
    return (
        f"Última corrida OK: {prev.get('corrida', '?')} "
        f"{_fmt_ts(prev.get('updated'))} | "
        f"ingresos {int(prev.get('completas') or 0)} completas"
    )


def horas_desde_ultima_ok(prev: dict[str, Any] | None) -> float | None:
    if not prev or (prev.get("status") or "").upper() not in ("OK", "PARTIAL"):
        base = prev.get("last_ok") if prev else None
        if not base:
            return None
        prev = base
    iso = prev.get("updated")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZONA_EC)
        delta = datetime.now(ZONA_EC) - dt.astimezone(ZONA_EC)
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


def evaluar_estado_corrida(
    *,
    resumen_desc: dict,
    resumen_proc: dict,
    fatal_error: str | None,
) -> tuple[str, int]:
    """
    Retorna (status, exit_code).
    status: OK | PARTIAL | FAILED
    exit_code: 0 solo sin errores; 1 si fallo o errores parciales.
    """
    if fatal_error:
        return "FAILED", 1

    err_desc = int(resumen_desc.get("errores") or 0)
    err_proc = int(resumen_proc.get("errores") or 0)
    if err_desc > 0 or err_proc > 0:
        return "PARTIAL", 1

    if resumen_desc.get("ejecutada") is False and resumen_proc.get("ejecutada") is False:
        return "FAILED", 1

    return "OK", 0


def construir_checkpoint(
    *,
    corrida: str,
    ventana: str,
    resumen_desc: dict,
    resumen_proc: dict,
    fatal_error: str | None,
    prev: dict[str, Any] | None,
) -> dict[str, Any]:
    status, _ = evaluar_estado_corrida(
        resumen_desc=resumen_desc,
        resumen_proc=resumen_proc,
        fatal_error=fatal_error,
    )
    now = datetime.now(ZONA_EC).isoformat()
    payload: dict[str, Any] = {
        "status": status,
        "corrida": corrida,
        "ventana": ventana,
        "updated": now,
        "portal_ejecutada": bool(resumen_desc.get("ejecutada")),
        "listados": int(resumen_desc.get("listados") or 0),
        "descargados": int(resumen_desc.get("descargados") or 0),
        "errores_descarga": int(resumen_desc.get("errores") or 0),
        "completas": int(resumen_proc.get("completas") or 0),
        "parciales": int(resumen_proc.get("parciales") or 0),
        "errores_proceso": int(resumen_proc.get("errores") or 0),
        "fatal": (fatal_error or "")[:500] or None,
    }
    if status in ("OK", "PARTIAL"):
        payload["last_ok"] = {**payload}
    elif prev and prev.get("last_ok"):
        payload["last_ok"] = prev["last_ok"]
    elif prev and (prev.get("status") or "").upper() in ("OK", "PARTIAL"):
        payload["last_ok"] = {
            k: prev.get(k)
            for k in (
                "status",
                "corrida",
                "updated",
                "completas",
                "parciales",
                "descargados",
                "ventana",
            )
        }
    return payload

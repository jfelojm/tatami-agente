"""
Evita que alertas automáticas (SRI, pipeline) interrumpan una conversación WA activa.
"""

from __future__ import annotations

import os
import time

_wa_last_inbound_at: dict[str, float] = {}


def _norm_wa(numero: str) -> str:
    return (numero or "").lstrip("+").strip()


def touch_wa_chat(wa_id: str) -> None:
    key = _norm_wa(wa_id)
    if key:
        _wa_last_inbound_at[key] = time.monotonic()


def chat_activo(wa_id: str, ventana_sec: float | None = None) -> bool:
    key = _norm_wa(wa_id)
    if not key:
        return False
    ts = _wa_last_inbound_at.get(key)
    if ts is None:
        return False
    ventana = ventana_sec
    if ventana is None:
        try:
            ventana = float(os.getenv("TATAMI_WA_ALERTA_SUPRIMIR_SEC", "120"))
        except ValueError:
            ventana = 120.0
    return (time.monotonic() - ts) < ventana

from __future__ import annotations

"""
Sesiones simples por WhatsApp (por número) para flujos guiados de factura.

Estado actual:
- In-memory (se reinicia al reiniciar el server).
- Suficiente para confirmar acciones tipo SI/NO/CANCELAR después de procesar un PDF.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class SesionFactura:
    telefono: str
    estado: str  # "confirmacion_pendiente" | ...
    creada_en: datetime
    expira_en: datetime
    payload: dict


_sesiones: dict[str, SesionFactura] = {}


def _ahora() -> datetime:
    return datetime.now()


def hay_sesion_activa(telefono: str) -> bool:
    t = (telefono or "").strip()
    if not t:
        return False
    s = _sesiones.get(t)
    if not s:
        return False
    if _ahora() > s.expira_en:
        _sesiones.pop(t, None)
        return False
    return True


def crear_sesion_confirmacion(telefono: str, *, payload: dict, ttl_min: int = 30) -> None:
    t = (telefono or "").strip()
    if not t:
        return
    now = _ahora()
    _sesiones[t] = SesionFactura(
        telefono=t,
        estado="confirmacion_pendiente",
        creada_en=now,
        expira_en=now + timedelta(minutes=max(5, int(ttl_min))),
        payload=payload or {},
    )


def leer_sesion(telefono: str) -> SesionFactura | None:
    if not hay_sesion_activa(telefono):
        return None
    return _sesiones.get((telefono or "").strip())


def cerrar_sesion(telefono: str) -> None:
    _sesiones.pop((telefono or "").strip(), None)


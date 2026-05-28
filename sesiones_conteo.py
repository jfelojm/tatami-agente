from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_sb: Client | None = None


def _get_sb() -> Client:
    global _sb
    if _sb is None:
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _sb


TABLE = "sesiones_conteo"


def _delta_coincide_busqueda(d: dict, cods_norm: list[str]) -> bool:
    """True si el fragmento del usuario aparece en nombre_mp o coincide cod_mp."""
    nombre = (d.get("nombre_mp") or "").upper()
    cod = str(d.get("cod_mp_sistema") or "").strip()
    return any(c in nombre for c in cods_norm) or cod in cods_norm


def crear_sesion(numero_wa: str, envio_id: str, ciclo_id: str, deltas: list[dict]) -> dict:
    sb = _get_sb()
    # Cerrar sesiones anteriores activas del mismo número
    sb.table(TABLE).update({"estado": "CERRADA"}).eq("numero_wa", numero_wa).eq(
        "estado", "PENDIENTE"
    ).execute()
    row = {
        "numero_wa": numero_wa,
        "envio_id": envio_id,
        "ciclo_id": ciclo_id,
        "deltas_pendientes": json.dumps(deltas),
        "aprobados": "[]",
        "rechazados": "[]",
        "estado": "PENDIENTE",
    }
    res = sb.table(TABLE).insert(row).execute()
    return res.data[0]


def get_sesion_activa(numero_wa: str) -> dict | None:
    sb = _get_sb()
    res = (
        sb.table(TABLE)
        .select("*")
        .eq("numero_wa", numero_wa)
        .eq("estado", "PENDIENTE")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def aprobar_items(sesion_id: str, cods: list[str] | None = None) -> dict:
    """cods=None significa aprobar todo."""
    sb = _get_sb()
    res = sb.table(TABLE).select("*").eq("id", sesion_id).execute()
    sesion = res.data[0]
    deltas = json.loads(sesion["deltas_pendientes"])
    aprobados = json.loads(sesion["aprobados"])

    if cods is None:
        aprobar = deltas
        restantes = []
    else:
        cods_norm = [c.upper().strip() for c in cods if c and str(c).strip()]
        aprobar = [d for d in deltas if _delta_coincide_busqueda(d, cods_norm)]
        restantes = [d for d in deltas if d not in aprobar]

    aprobados.extend(aprobar)
    upd: dict[str, Any] = {
        "aprobados": json.dumps(aprobados),
        "deltas_pendientes": json.dumps(restantes),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if not restantes:
        upd["estado"] = "APROBADA"
    sb.table(TABLE).update(upd).eq("id", sesion_id).execute()
    return {"aprobados": aprobar, "pendientes": restantes}


def rechazar_items(sesion_id: str, cods: list[str] | None = None) -> dict:
    """cods=None significa rechazar todo."""
    sb = _get_sb()
    res = sb.table(TABLE).select("*").eq("id", sesion_id).execute()
    sesion = res.data[0]
    deltas = json.loads(sesion["deltas_pendientes"])
    rechazados = json.loads(sesion["rechazados"])

    if cods is None:
        rechazar = deltas
        restantes = []
    else:
        cods_norm = [c.upper().strip() for c in cods if c and str(c).strip()]
        rechazar = [d for d in deltas if _delta_coincide_busqueda(d, cods_norm)]
        restantes = [d for d in deltas if d not in rechazar]

    rechazados.extend(rechazar)
    upd: dict[str, Any] = {
        "rechazados": json.dumps(rechazados),
        "deltas_pendientes": json.dumps(restantes),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if not restantes:
        upd["estado"] = "CERRADA"
    sb.table(TABLE).update(upd).eq("id", sesion_id).execute()
    return {"rechazados": rechazar, "pendientes": restantes}


def cerrar_sesion(sesion_id: str) -> None:
    _get_sb().table(TABLE).update(
        {"estado": "CERRADA", "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", sesion_id).execute()

"""
Evita corridas duplicadas de pipelines programados (misma franja horaria / mismo día).

- Lock de proceso: una sola instancia en ejecución.
- Slot completado: no repetir la misma hora (pipeline horario) o el mismo día (digest).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
ZONA_EC = ZoneInfo("America/Guayaquil")


def _ahora_ec() -> datetime:
    return datetime.now(ZONA_EC)


def minutos_desde_inicio_hora(dt: datetime | None = None) -> int:
    """Minutos transcurridos desde :00 de la hora calendario EC."""
    now = dt or _ahora_ec()
    return now.minute


def corrida_fuera_de_tolerancia(
    *,
    hora_esperada: int | None = None,
    minuto_esperado: int = 0,
    tolerancia_min: int = 8,
) -> tuple[bool, str]:
    """
    True si la corrida llegó demasiado tarde respecto al slot programado.
    hora_esperada=None → usa la hora calendario actual (pipeline horario en punto).
    """
    now = _ahora_ec()
    h = hora_esperada if hora_esperada is not None else now.hour
    if now.hour != h:
        return True, (
            f"hora actual {now.hour:02d}:{now.minute:02d} != franja programada H{h:02d}:{minuto_esperado:02d}"
        )
    if now.minute > max(0, tolerancia_min):
        return True, (
            f"inicio tardío {now.hour:02d}:{now.minute:02d} "
            f"(máx +{tolerancia_min} min tras H{h:02d}:{minuto_esperado:02d})"
        )
    return False, ""


def _lock_path(nombre: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in nombre)
    return LOGS / f"{safe}.lock"


def _slots_path(nombre: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in nombre)
    return LOGS / f"{safe}_slots.json"


def lock_activo(nombre: str, *, ttl_min: int = 120) -> bool:
    """True si hay otra instancia en curso (lock fresco)."""
    p = _lock_path(nombre)
    if not p.is_file():
        return False
    try:
        age = _ahora_ec() - datetime.fromtimestamp(p.stat().st_mtime, ZONA_EC)
        if age > timedelta(minutes=ttl_min):
            return False
    except OSError:
        return False
    return True


def slot_ya_completado(
    slots_name: str,
    slot_id: str,
    *,
    ttl_horas: float | None = None,
) -> bool:
    """
    True si slot_id ya terminó OK.
    ttl_horas: si se define, ignora entradas más viejas (p. ej. digest de otro día).
    """
    p = _slots_path(slots_name)
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    row = data.get(slot_id) or {}
    if str(row.get("status") or "").upper() != "OK":
        return False
    updated = row.get("updated")
    if ttl_horas is not None and updated:
        try:
            ts = datetime.fromisoformat(str(updated))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ZONA_EC)
            if (_ahora_ec() - ts.astimezone(ZONA_EC)) > timedelta(hours=ttl_horas):
                return False
        except (TypeError, ValueError):
            pass
    return True


def marcar_slot_completado(
    slots_name: str,
    slot_id: str,
    *,
    status: str = "OK",
    detalle: str = "",
) -> None:
    p = _slots_path(slots_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data[slot_id] = {
        "status": status,
        "updated": _ahora_ec().isoformat(),
        "detalle": (detalle or "")[:200],
    }
    # Mantener últimos ~48 slots
    if len(data) > 48:
        orden = sorted(
            data.items(),
            key=lambda kv: str((kv[1] or {}).get("updated") or ""),
        )
        data = dict(orden[-48:])
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def corrida_unica(
    lock_name: str,
    *,
    slot_id: str | None = None,
    slots_name: str | None = None,
    forzar: bool = False,
    lock_ttl_min: int = 120,
):
    """
    Context manager: adquiere lock; al salir OK marca slot completado.

    Si lock activo o slot ya OK (y no forzar), imprime y hace yield False (omitir).
    Si adquiere lock, yield True (ejecutar).
    """
    omitir = False
    motivo = ""

    if not forzar and lock_activo(lock_name, ttl_min=lock_ttl_min):
        omitir = True
        motivo = f"otra instancia de {lock_name} en curso (lock activo)"
    elif (
        not forzar
        and slot_id
        and slots_name
        and slot_ya_completado(slots_name, slot_id)
    ):
        omitir = True
        motivo = f"slot {slot_id} ya completado OK (omitido; use --forzar para repetir)"

    if omitir:
        print(f"  INFO pipeline_run_guard: {motivo}")
        yield False
        return

    LOGS.mkdir(parents=True, exist_ok=True)
    lp = _lock_path(lock_name)
    try:
        lp.write_text(
            f"pid={os.getpid()}\nstarted={_ahora_ec().isoformat()}\nslot={slot_id or ''}\n",
            encoding="utf-8",
        )
    except OSError as e:
        print(f"  WARN: no se pudo crear lock {lp}: {e}")

    try:
        yield True
    finally:
        try:
            if lp.is_file():
                lp.unlink()
        except OSError:
            pass

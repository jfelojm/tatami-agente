"""Completitud de carga ventas: grid Smart Menu vs hist_ventas (por id_documento)."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supabase import Client

_COD_VENTA_RE = re.compile(r"^VTA-\d{8}-(\d+)-\d+$")


def id_documento_desde_cod_venta(cod_venta: str) -> str:
    m = _COD_VENTA_RE.match((cod_venta or "").strip())
    return m.group(1) if m else ""


def id_documentos_desde_grid_rows(rows: list[list[str]]) -> set[str]:
    from ventas_smartmenu import _venta_header_from_row

    out: set[str] = set()
    for row in rows:
        idd = (_venta_header_from_row(row).get("id_documento") or "").strip()
        if idd:
            out.add(idd)
    return out


def id_documentos_hist(sb: Client, fecha: str) -> set[str]:
    fecha = (fecha or "").strip().split()[0]
    out: set[str] = set()
    offset = 0
    while True:
        chunk = (
            sb.table("hist_ventas")
            .select("cod_venta")
            .eq("fecha", fecha)
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        for row in chunk:
            idd = id_documento_desde_cod_venta(row.get("cod_venta") or "")
            if idd:
                out.add(idd)
        if len(chunk) < 1000:
            break
        offset += 1000
    return out


def auditar_completitud(
    fecha: str,
    grid_ids: set[str],
    *,
    sb: Client | None = None,
) -> dict:
    fecha = (fecha or "").strip().split()[0]
    if sb is None:
        from ventas_smartmenu import supabase as sb_default

        sb = sb_default

    hist_ids = id_documentos_hist(sb, fecha)
    faltantes = sorted(grid_ids - hist_ids, key=lambda x: int(x) if x.isdigit() else x)
    sobrantes = sorted(hist_ids - grid_ids, key=lambda x: int(x) if x.isdigit() else x)
    return {
        "fecha": fecha,
        "grid_docs": len(grid_ids),
        "hist_docs": len(hist_ids),
        "faltantes": faltantes,
        "sobrantes": sobrantes,
        "ok": not faltantes and not sobrantes,
    }


def mensaje_completitud(rep: dict, *, max_listar: int = 8) -> str:
    if rep.get("ok"):
        return f"Completitud OK: {rep['grid_docs']} documentos grid = hist."
    parts = [
        f"CARGA INCOMPLETA fecha {rep.get('fecha')}: "
        f"grid={rep.get('grid_docs')} docs, hist={rep.get('hist_docs')} docs."
    ]
    falt = rep.get("faltantes") or []
    sob = rep.get("sobrantes") or []
    if falt:
        muestra = ", ".join(falt[:max_listar])
        extra = f" (+{len(falt) - max_listar} más)" if len(falt) > max_listar else ""
        parts.append(f"Faltan en hist_ventas (id_documento): {muestra}{extra}")
    if sob:
        muestra = ", ".join(sob[:max_listar])
        extra = f" (+{len(sob) - max_listar} más)" if len(sob) > max_listar else ""
        parts.append(f"En hist pero no en grid hoy: {muestra}{extra}")
    return " ".join(parts)


def exigir_completitud() -> bool:
    raw = (os.getenv("VENTAS_EXIGIR_COMPLETITUD") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def auditar_fecha_remota(fecha: str, *, sb: Client | None = None) -> dict:
    """Completitud grid Smart Menu vs hist_ventas para una fecha."""
    from ventas_smartmenu import descargar_ventas_grid, supabase as sb_default

    fecha = (fecha or "").strip().split()[0]
    sb = sb or sb_default
    rows = descargar_ventas_grid(fecha)
    if not rows:
        return {
            "fecha": fecha,
            "grid_docs": 0,
            "hist_docs": 0,
            "faltantes": [],
            "sobrantes": [],
            "ok": True,
            "sin_ventas": True,
        }
    grid_ids = id_documentos_desde_grid_rows(rows)
    rep = auditar_completitud(fecha, grid_ids, sb=sb)
    rep["sin_ventas"] = False
    return rep


def asegurar_ventas_dia(fecha: str) -> dict:
    """
    Si hist_ventas está incompleto vs grid, vuelve a ejecutar ventas_smartmenu
    (inserta solo faltantes; no borra lo ya cargado).
    """
    import subprocess
    import sys
    from pathlib import Path

    rep = auditar_fecha_remota(fecha)
    if rep.get("ok") or rep.get("sin_ventas"):
        return rep

    root = Path(__file__).resolve().parent
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [sys.executable, str(root / "ventas_smartmenu.py"), "--fecha", fecha],
        cwd=str(root),
        env=env,
    )
    rep2 = auditar_fecha_remota(fecha)
    rep2["ventas_exit"] = proc.returncode
    rep2["reparado"] = proc.returncode == 0 and bool(rep2.get("ok"))
    return rep2


def dias_con_huecos_recientes(dias: int = 3) -> list[str]:
    """Días cerrados con ventas en Smart Menu pero documentos faltantes en hist."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    zona = ZoneInfo("America/Guayaquil")
    hoy = datetime.now(zona).date()
    out: list[str] = []
    for i in range(1, max(1, dias) + 1):
        f = (hoy - timedelta(days=i)).isoformat()
        rep = auditar_fecha_remota(f)
        if rep.get("sin_ventas"):
            continue
        if not rep.get("ok") and (rep.get("faltantes") or []):
            out.append(f)
    return out

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

"""Indicadores históricos de rotura / merma en inventario."""

from __future__ import annotations

from collections import defaultdict
from datetime import date


def _to_float(v: object) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _clave_agrup(fecha: str, agrup: str) -> str:
    from datetime import date as dt

    d = dt.fromisoformat(fecha[:10])
    if agrup == "dia":
        return fecha[:10]
    if agrup == "semana":
        return d.strftime("%G-W%V")
    if agrup == "anio":
        return fecha[:4]
    return fecha[:7]


def build_roturas_historico(
    movs: list[dict],
    *,
    desde: date,
    hasta: date,
    agrup: str = "mes",
    bodega: str | None = None,
) -> dict:
    from bodegas_config import normalizar_cod_bodega

    bod_map = {"COCINA": "BOD-001", "BARRA": "BOD-002"}
    bod_f = bod_map.get((bodega or "").upper())

    serie: dict[str, float] = defaultdict(float)
    por_mp: dict[str, dict] = defaultdict(lambda: {"nombre_mp": "", "vta": 0.0, "uds": 0.0})
    total = 0.0
    lineas = 0

    for m in movs:
        tipo = (m.get("tipo_mov") or "").strip().upper()
        if tipo not in ("AJUSTE_NEGATIVO", "AJUSTE_POSITIVO"):
            continue
        if tipo != "AJUSTE_NEGATIVO":
            continue
        fecha = (str(m.get("fecha") or ""))[:10]
        if not fecha or fecha < desde.isoformat() or fecha > hasta.isoformat():
            continue
        bod = normalizar_cod_bodega(m.get("cod_bodega_origen") or m.get("cod_bodega_destino"))
        if bod_f and bod != bod_f:
            continue
        ct = abs(_to_float(m.get("costo_total")))
        qty = abs(_to_float(m.get("cantidad_mov")))
        key = _clave_agrup(fecha, agrup)
        serie[key] += ct
        total += ct
        lineas += 1
        cod = (m.get("cod_mp_sistema") or "").strip()
        por_mp[cod]["nombre_mp"] = (m.get("nombre_mp") or cod).strip()
        por_mp[cod]["vta"] += ct
        por_mp[cod]["uds"] += qty

    labels = sorted(serie.keys())
    top = sorted(
        [{"cod_mp_sistema": k, **v, "vta": round(v["vta"], 2)} for k, v in por_mp.items()],
        key=lambda x: x["vta"],
        reverse=True,
    )[:20]

    return {
        "periodo": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "agrup": agrup,
        "labels": labels,
        "valores": [round(serie[k], 2) for k in labels],
        "total_rotura": round(total, 2),
        "lineas_ajuste": lineas,
        "top_mps": top,
        "nota": "Rotura contable = AJUSTE_NEGATIVO por conteos. Quiebre operativo requiere snapshot diario (fase 5b).",
    }

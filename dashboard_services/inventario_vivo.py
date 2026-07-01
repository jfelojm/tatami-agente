"""Estado de inventario en vivo — bodegas 001/002/003/005."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from bodegas_config import BODEGAS, normalizar_cod_bodega
from inventario_stock_mp import norm_mp
from recalcular_stock_sheets import _clave_stock, build_stock_calculado


# Dashboard: solo bodegas operativas con nombre visible
BODEGAS_INV = ("BOD-001", "BOD-002", "BOD-003", "BOD-005")

# Alias para confianza.py y otros consumidores legacy
BODEGAS_DASH = {BODEGAS[b].nombre: b for b in BODEGAS_INV if b in BODEGAS}

RESPONSABILIDAD: dict[str, str] = {
    "BOD-001": "Cocina",
    "BOD-005": "Cocina",
    "BOD-002": "Barra",
    "BOD-003": "Barra",
}


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or default)
    except (TypeError, ValueError):
        return default


def _dias_compra(row: dict) -> float:
    d = _to_float(row.get("dias_cobertura_par"))
    if d > 0:
        return d
    return _to_float(row.get("dias_seguridad"), 7.0) or 7.0


def _perdida_falta_stock(stock: float, par: float, consumo: float, costo: float) -> float:
    """Pérdida estimada por falta: consumo diario × costo unitario si quiebre o bajo PAR."""
    if consumo <= 0 or costo <= 0:
        return 0.0
    if stock <= 0:
        return round(consumo * costo, 2)
    if par > 0 and stock < par:
        deficit = min(consumo, par - stock) if consumo > 0 else (par - stock)
        return round(max(0.0, deficit) * costo, 2)
    return 0.0


def clasificar_fila(
    *,
    stock: float,
    par: float,
    consumo_diario: float,
    dias_compra: float,
    costo_unit: float,
) -> dict[str, Any]:
    ratio = stock / par if par > 0 else (1.0 if stock > 0 else 0.0)
    dias_cob = stock / consumo_diario if consumo_diario > 0 else (999.0 if stock > 0 else 0.0)
    exceso = max(0.0, stock - par) if par > 0 else 0.0
    costo_oportunidad = round(exceso * costo_unit, 2)
    perdida = _perdida_falta_stock(stock, par, consumo_diario, costo_unit)

    estado = "OK"
    if stock < 0:
        estado = "NEGATIVO"
    elif stock <= 0 and consumo_diario > 0:
        estado = "QUIEBRE"
    elif par > 0 and ratio < 0.5:
        estado = "CRITICO"
    elif par > 0 and ratio < 1.0:
        estado = "BAJO_PAR"
    elif dias_compra > 0 and consumo_diario > 0 and dias_cob >= 2 * dias_compra:
        estado = "SOBRE_CRITICO"
    elif par > 0 and stock >= 1.5 * par:
        estado = "SOBRE_ALERTA"

    return {
        "estado": estado,
        "stock": round(stock, 4),
        "par_level": round(par, 4),
        "ratio_par_pct": round(ratio * 100, 1) if par > 0 else None,
        "dias_cobertura": round(dias_cob, 1) if consumo_diario > 0 else None,
        "dias_compra_config": round(dias_compra, 1),
        "costo_oportunidad": costo_oportunidad,
        "perdida_venta_est": perdida,
        "consumo_diario": round(consumo_diario, 4),
    }


def build_inventario_vivo(
    rows_mp: list[dict],
    stock_map: dict | None = None,
    *,
    responsabilidad: str | None = None,
    cod_bodega: str | None = None,
    dias_periodo: int = 1,
) -> dict:
    stock_map = stock_map or build_stock_calculado()
    resp_f = (responsabilidad or "").strip().capitalize()
    bod_f = normalizar_cod_bodega(cod_bodega) if cod_bodega else ""

    meta_mp: dict[str, dict] = {}
    for r in rows_mp:
        cod = norm_mp(r.get("cod_mp_sistema"))
        bod = normalizar_cod_bodega(r.get("cod_bodega"))
        if not cod or bod not in BODEGAS_INV:
            continue
        if bod_f and bod != bod_f:
            continue
        resp = RESPONSABILIDAD.get(bod, "Otro")
        if resp_f and resp != resp_f:
            continue
        key = (cod, bod)
        if key not in meta_mp:
            meta_mp[key] = {
                "cod_mp_sistema": cod,
                "nombre_mp": (r.get("nombre_mp") or cod).strip(),
                "categoria": (r.get("categoria") or "Sin categoría").strip(),
                "unidad_base": (r.get("unidad_base") or "").strip(),
                "par_level": _to_float(r.get("par_level")),
                "consumo_diario_calculado": _to_float(r.get("consumo_diario_calculado")),
                "dias_compra": _dias_compra(r),
                "costo_unitario_ref": _to_float(r.get("costo_unitario_ref")),
                "cod_bodega": bod,
                "nombre_bodega": BODEGAS[bod].nombre if bod in BODEGAS else bod,
                "responsabilidad": resp,
            }

    items: list[dict] = []
    resumen_estados: dict[str, int] = defaultdict(int)
    costo_oportunidad = 0.0
    perdida_total = 0.0

    for (_cod, bod), meta in meta_mp.items():
        stock = float(stock_map.get(_clave_stock(_cod, bod), 0.0))
        info = clasificar_fila(
            stock=stock,
            par=meta["par_level"],
            consumo_diario=meta["consumo_diario_calculado"],
            dias_compra=meta["dias_compra"],
            costo_unit=meta["costo_unitario_ref"],
        )
        if stock == 0 and meta["par_level"] <= 0 and meta["consumo_diario_calculado"] <= 0:
            continue
        perdida_periodo = round(info["perdida_venta_est"] * dias_periodo, 2)
        oportunidad_periodo = round(info["costo_oportunidad"] * dias_periodo, 2)
        row = {
            **meta,
            **info,
            "perdida_periodo": perdida_periodo,
            "oportunidad_periodo": oportunidad_periodo,
        }
        items.append(row)
        resumen_estados[info["estado"]] += 1
        costo_oportunidad += oportunidad_periodo
        perdida_total += perdida_periodo

    orden = {
        "NEGATIVO": 0, "QUIEBRE": 1, "CRITICO": 2, "BAJO_PAR": 3,
        "SOBRE_CRITICO": 4, "SOBRE_ALERTA": 5, "OK": 6,
    }
    items.sort(key=lambda x: (
        {"Cocina": 0, "Barra": 1}.get(x["responsabilidad"], 2),
        x["nombre_bodega"],
        x.get("categoria", ""),
        orden.get(x["estado"], 9),
    ))

    arbol = _build_arbol(items)

    return {
        "items": items,
        "arbol": arbol,
        "resumen": dict(resumen_estados),
        "costo_oportunidad_total": round(costo_oportunidad, 2),
        "perdida_venta_total": round(perdida_total, 2),
        "consolidado": {
            "costo_oportunidad": round(costo_oportunidad, 2),
            "perdida_venta": round(perdida_total, 2),
            "dias_periodo": dias_periodo,
        },
        "total_items": len(items),
        "bodegas": [
            {"cod": b, "nombre": BODEGAS[b].nombre, "responsabilidad": RESPONSABILIDAD.get(b, "")}
            for b in BODEGAS_INV
        ],
    }


def _build_arbol(items: list[dict]) -> list[dict]:
    """Responsabilidad → bodega → categoría → MPs."""
    tree: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for it in items:
        tree[it["responsabilidad"]][it["nombre_bodega"]][it["categoria"]].append(it)

    out = []
    for resp in sorted(tree.keys()):
        bod_nodes = []
        for bod_n in sorted(tree[resp].keys()):
            cat_nodes = []
            for cat in sorted(tree[resp][bod_n].keys()):
                cat_nodes.append({
                    "tipo": "categoria",
                    "nombre": cat,
                    "items": tree[resp][bod_n][cat],
                })
            bod_nodes.append({"tipo": "bodega", "nombre": bod_n, "categorias": cat_nodes})
        out.append({"tipo": "responsabilidad", "nombre": resp, "bodegas": bod_nodes})
    return out

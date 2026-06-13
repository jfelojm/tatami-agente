"""Agregación de compras de inventario para dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from bodegas_config import normalizar_cod_bodega
from dashboard_services.periodos import (
    acumulado_anio,
    mismo_periodo_anio_anterior,
    periodo_anterior,
    resumen_comparativo,
)
from inventario_stock_mp import norm_mp


# BOD-001, BOD-005 = Cocina · BOD-002, BOD-003 = Barra
_BOD_COCINA = frozenset({"BOD-001", "BOD-005"})
_BOD_BARRA = frozenset({"BOD-002", "BOD-003"})


def _to_float(v: object) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _es_entrada_compra(row: dict) -> bool:
    return (row.get("tipo_mov") or "").strip().upper() == "ENTRADA"


def _area_bodega(cod_bod: str) -> str:
    b = normalizar_cod_bodega(cod_bod)
    if b in _BOD_COCINA:
        return "COCINA"
    if b in _BOD_BARRA:
        return "BARRA"
    return "OTRO"


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


def _mapa_proveedores_nombre(prov_rows: list[dict]) -> dict[str, str]:
    """RUC -> razón social (nunca mostrar RUC como nombre)."""
    out: dict[str, str] = {}
    for p in prov_rows:
        ruc = (p.get("ruc") or p.get("RUC") or "").strip()
        nombre = (p.get("razon_social") or p.get("Razon_social") or "").strip()
        if ruc and nombre:
            out[ruc] = nombre
    return out


def _mapa_mp_area(rows_mp: list[dict]) -> dict[str, str]:
    """cod_mp -> área default desde BD_MP_SISTEMA."""
    out: dict[str, str] = {}
    for r in rows_mp:
        cod = norm_mp(r.get("cod_mp_sistema"))
        if not cod:
            continue
        bod = normalizar_cod_bodega(r.get("cod_bodega"))
        area = _area_bodega(bod)
        if cod not in out or area != "OTRO":
            out[cod] = area
    return out


def _nombre_proveedor(
    ruc: str,
    meta_factura: dict,
    prov_por_ruc: dict[str, str],
) -> str:
    ruc = (ruc or "").strip()
    if ruc and ruc in prov_por_ruc:
        return prov_por_ruc[ruc]
    rs = (meta_factura.get("razon_social") or "").strip()
    if rs and not rs.isdigit() and len(rs) > 8:
        return rs
    return "Proveedor sin nombre"


def _metricas_compras(
    rows: list[dict],
    facturas: list[dict],
    prov_por_ruc: dict[str, str],
    mps_validos: set[str],
    mp_area: dict[str, str],
    *,
    agrup: str = "mes",
    area_filtro: str | None = None,
) -> dict:
    fact_por_num: dict[str, dict] = {}
    for f in facturas:
        num = (f.get("num_factura") or "").strip()
        if num:
            fact_por_num[num] = f

    total = 0.0
    por_prov: dict[str, dict] = defaultdict(lambda: {"vta": 0.0, "lineas": 0, "facturas": set()})
    por_area: dict[str, float] = {"COCINA": 0.0, "BARRA": 0.0, "OTRO": 0.0}
    por_mp: dict[str, dict] = {}
    serie: dict[str, float] = defaultdict(float)
    lineas = 0

    for r in rows:
        if not _es_entrada_compra(r):
            continue
        cod = norm_mp(r.get("cod_mp_sistema"))
        if mps_validos and cod not in mps_validos:
            continue
        bod = normalizar_cod_bodega(r.get("cod_bodega_destino") or r.get("cod_bodega_origen"))
        area = _area_bodega(bod) if bod else mp_area.get(cod, "OTRO")
        if area_filtro and area != area_filtro:
            continue

        ct = _to_float(r.get("costo_total"))
        total += ct
        lineas += 1
        por_area[area] += ct

        num = (r.get("num_documento") or "").strip()
        fac = fact_por_num.get(num, {})
        ruc = (fac.get("ruc_proveedor") or "").strip()
        prov = _nombre_proveedor(ruc, fac, prov_por_ruc)
        por_prov[prov]["vta"] += ct
        por_prov[prov]["lineas"] += 1
        if num:
            por_prov[prov]["facturas"].add(num)

        fecha = (str(r.get("fecha") or ""))[:10]
        if fecha:
            serie[_clave_agrup(fecha, agrup)] += ct

        if cod not in por_mp:
            por_mp[cod] = {
                "nombre_mp": (r.get("nombre_mp") or cod).strip(),
                "area": area,
                "vta": 0.0,
                "uds": 0.0,
            }
        por_mp[cod]["vta"] += ct
        por_mp[cod]["uds"] += _to_float(r.get("cantidad_mov"))

    proveedores = sorted(
        [
            {
                "nombre": k,
                "vta": round(v["vta"], 2),
                "lineas": v["lineas"],
                "facturas": len(v["facturas"]),
                "pct": round(v["vta"] / total * 100, 1) if total else 0,
            }
            for k, v in por_prov.items()
        ],
        key=lambda x: x["vta"],
        reverse=True,
    )
    mps = sorted(
        [{"cod_mp_sistema": k, **v, "vta": round(v["vta"], 2)} for k, v in por_mp.items()],
        key=lambda x: x["vta"],
        reverse=True,
    )
    labels = sorted(serie.keys())
    return {
        "vta": round(total, 2),
        "lineas": lineas,
        "por_area": {k: round(v, 2) for k, v in por_area.items()},
        "proveedores": proveedores,
        "top_proveedores": proveedores[:15],
        "materias_primas": mps[:50],
        "labels": labels,
        "serie": [round(serie[k], 2) for k in labels],
    }


def build_resumen_compras(
    *,
    query_mov_fn,
    query_facturas_fn,
    rows_mp: list[dict],
    rows_prov: list[dict],
    desde: date,
    hasta: date,
    agrup: str = "mes",
    area: str | None = None,
) -> dict:
    mps_validos = {norm_mp(r.get("cod_mp_sistema")) for r in rows_mp if norm_mp(r.get("cod_mp_sistema"))}
    mp_area = _mapa_mp_area(rows_mp)
    prov_por_ruc = _mapa_proveedores_nombre(rows_prov)

    rows = query_mov_fn(desde.isoformat(), hasta.isoformat())
    facturas = query_facturas_fn(desde.isoformat(), hasta.isoformat())
    actual = _metricas_compras(
        rows, facturas, prov_por_ruc, mps_validos, mp_area, agrup=agrup, area_filtro=area
    )

    ini_a, fin_a = periodo_anterior(desde, hasta)
    ant = _metricas_compras(
        query_mov_fn(ini_a.isoformat(), fin_a.isoformat()),
        facturas,
        prov_por_ruc,
        mps_validos,
        mp_area,
        agrup=agrup,
        area_filtro=area,
    )

    ini_y, fin_y = mismo_periodo_anio_anterior(desde, hasta)
    ya = _metricas_compras(
        query_mov_fn(ini_y.isoformat(), fin_y.isoformat()),
        facturas,
        prov_por_ruc,
        mps_validos,
        mp_area,
        agrup=agrup,
        area_filtro=area,
    )

    ini_ytd, fin_ytd = acumulado_anio(hasta)
    ytd = _metricas_compras(
        query_mov_fn(ini_ytd.isoformat(), fin_ytd.isoformat()),
        facturas,
        prov_por_ruc,
        mps_validos,
        mp_area,
        agrup=agrup,
        area_filtro=area,
    )

    parciales = sum(1 for f in facturas if (f.get("estado") or "").upper() == "PARCIAL")

    return {
        "periodo": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "agrup": agrup,
        "actual": actual,
        "comparativo_anterior": {
            "periodo": {"desde": ini_a.isoformat(), "hasta": fin_a.isoformat()},
            "metricas": resumen_comparativo(
                {"vta": actual["vta"], "uds": actual["lineas"], "ticket": 0, "descuentos": 0},
                {"vta": ant["vta"], "uds": ant["lineas"], "ticket": 0, "descuentos": 0},
            ),
        },
        "acumulado_anio": {"vta": ytd["vta"], "lineas": ytd["lineas"]},
        "facturas_parciales": parciales,
    }

"""Agregación de compras de inventario para dashboard."""

from __future__ import annotations

import re
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
from procesar_facturas_drive import COD_MP_SIN_CLASIFICAR


# Bodegas operativas de inventario
_BOD_INVENTARIO = frozenset({"BOD-001", "BOD-002", "BOD-003", "BOD-005"})
# BOD-001, BOD-005 = Cocina · BOD-002, BOD-003 = Barra
_BOD_COCINA = frozenset({"BOD-001", "BOD-005"})
_BOD_BARRA = frozenset({"BOD-002", "BOD-003"})
_TIPOS_PROV_INV = frozenset({"BARRA", "COCINA"})


def _to_float(v: object) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _es_entrada_compra(row: dict) -> bool:
    return (row.get("tipo_mov") or "").strip().upper() in ("ENTRADA", "ENTRADA_COSTO_HIST")


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


def _ruc_claves(ruc: str) -> list[str]:
    raw = (ruc or "").strip()
    if not raw:
        return []
    digits = re.sub(r"\D", "", raw)
    keys = [raw]
    if digits:
        keys.append(digits)
        if len(digits) == 10:
            keys.append(digits + "001")
    return keys


def _mapa_proveedores_inventario(prov_rows: list[dict]) -> dict[str, str]:
    """Claves RUC -> razón social solo proveedores Tipo Barra/Cocina en BD_PROV."""
    out: dict[str, str] = {}
    for p in prov_rows:
        tipo = (p.get("Tipo") or p.get("tipo") or "").strip().upper()
        if tipo not in _TIPOS_PROV_INV:
            continue
        nombre = str(p.get("razon_social") or p.get("Razon_social") or "").strip()
        ruc = str(p.get("ruc") or p.get("RUC") or "").strip()
        if not nombre or not ruc:
            continue
        for key in _ruc_claves(ruc):
            out[key] = nombre
    return out


def _nombre_proveedor_inventario(ruc: str, prov_inv: dict[str, str]) -> str | None:
    for key in _ruc_claves(ruc):
        if key in prov_inv:
            return prov_inv[key]
    return None


def _mapa_mp_area(rows_mp: list[dict]) -> dict[str, str]:
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


def _proveedor_dashboard(
    row: dict,
    fact_por_num: dict[str, dict],
    prov_inv: dict[str, str],
) -> str | None:
    """Nombre proveedor: BD_PROV inventario o razón social de la factura."""
    ruc = _ruc_desde_mov(row, fact_por_num)
    nombre = _nombre_proveedor_inventario(ruc, prov_inv)
    if nombre:
        return nombre
    num = (row.get("num_documento") or "").strip()
    fac = fact_por_num.get(num, {})
    razon = (fac.get("razon_social") or fac.get("meta", {}).get("razon_social") or "").strip()
    if razon:
        return razon
    obs = row.get("observaciones") or ""
    m = re.search(r"Proveedor:([^|]+)", obs, re.I)
    if m:
        return m.group(1).strip()
    return None


def _es_compra_inventario_dashboard(
    row: dict,
    *,
    mps_validos: set[str],
    prov_inv: dict[str, str],
    fact_por_num: dict[str, dict],
) -> bool:
    """Solo MPs en BD_MP_SISTEMA. Excluye MP 000 y sin catálogo."""
    if not _es_entrada_compra(row):
        return False
    cod = norm_mp(row.get("cod_mp_sistema"))
    if not cod or cod == COD_MP_SIN_CLASIFICAR or cod not in mps_validos:
        return False
    obs = (row.get("observaciones") or "").upper()
    if "APPROX_SIN_CATALOGO" in obs or "SIN_CATALOGO" in obs:
        return False
    bod = normalizar_cod_bodega(row.get("cod_bodega_destino") or row.get("cod_bodega_origen"))
    if bod not in _BOD_INVENTARIO:
        return False
    if _to_float(row.get("costo_total")) <= 0:
        return False
    return True


def _ruc_desde_mov(row: dict, fact_por_num: dict[str, dict]) -> str:
    obs = row.get("observaciones") or ""
    m = re.search(r"RUC:([0-9]{10,13})", obs, re.I)
    if m:
        return m.group(1).strip()
    num = (row.get("num_documento") or "").strip()
    fac = fact_por_num.get(num, {})
    return (fac.get("ruc_proveedor") or "").strip()


def _etiquetas_periodo(desde: date, hasta: date, agrup: str) -> list[str]:
    """Etiquetas continuas del rango (meses/semanas/años) para el gráfico."""
    from datetime import timedelta

    if agrup == "dia":
        labels: list[str] = []
        d = desde
        while d <= hasta:
            labels.append(d.isoformat())
            d += timedelta(days=1)
        return labels
    if agrup == "anio":
        return [str(y) for y in range(desde.year, hasta.year + 1)]
    if agrup == "semana":
        from datetime import timedelta

        seen: list[str] = []
        d = desde
        while d <= hasta:
            key = d.strftime("%G-W%V")
            if not seen or seen[-1] != key:
                seen.append(key)
            d += timedelta(days=1)
        return seen
    labels: list[str] = []
    y, m = desde.year, desde.month
    while (y, m) <= (hasta.year, hasta.month):
        labels.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return labels


def _metricas_compras(
    rows: list[dict],
    facturas: list[dict],
    prov_inv: dict[str, str],
    mps_validos: set[str],
    mp_area: dict[str, str],
    *,
    agrup: str = "mes",
    area_filtro: str | None = None,
    fill_labels: list[str] | None = None,
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
    serie_barra: dict[str, float] = defaultdict(float)
    serie_cocina: dict[str, float] = defaultdict(float)
    lineas = 0

    for r in rows:
        if not _es_compra_inventario_dashboard(
            r, mps_validos=mps_validos, prov_inv=prov_inv, fact_por_num=fact_por_num
        ):
            continue

        cod = norm_mp(r.get("cod_mp_sistema"))
        bod = normalizar_cod_bodega(r.get("cod_bodega_destino") or r.get("cod_bodega_origen"))
        area = _area_bodega(bod)
        if area_filtro and area != area_filtro:
            continue

        num = (r.get("num_documento") or "").strip()
        ruc = _ruc_desde_mov(r, fact_por_num)
        prov = _proveedor_dashboard(r, fact_por_num, prov_inv)
        if not prov:
            continue

        ct = _to_float(r.get("costo_total"))

        total += ct
        lineas += 1
        por_area[area] += ct
        por_prov[prov]["vta"] += ct
        por_prov[prov]["lineas"] += 1
        if num:
            por_prov[prov]["facturas"].add(num)

        fecha = (str(r.get("fecha") or ""))[:10]
        if fecha:
            key = _clave_agrup(fecha, agrup)
            serie[key] += ct
            if area == "BARRA":
                serie_barra[key] += ct
            elif area == "COCINA":
                serie_cocina[key] += ct

        if cod not in por_mp:
            por_mp[cod] = {
                "nombre_mp": (r.get("nombre_mp") or cod or "Sin clasificar").strip(),
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
    labels = fill_labels or sorted(set(serie.keys()) | set(serie_barra.keys()) | set(serie_cocina.keys()))
    return {
        "vta": round(total, 2),
        "lineas": lineas,
        "por_area": {k: round(v, 2) for k, v in por_area.items()},
        "proveedores": proveedores,
        "top_proveedores": proveedores[:15],
        "materias_primas": mps[:50],
        "labels": labels,
        "serie": [round(serie.get(k, 0.0), 2) for k in labels],
        "serie_barra": [round(serie_barra.get(k, 0.0), 2) for k in labels],
        "serie_cocina": [round(serie_cocina.get(k, 0.0), 2) for k in labels],
    }


def listar_facturas_inventario_dashboard(
    rows: list[dict],
    facturas: list[dict],
    prov_inv: dict[str, str],
    mps_validos: set[str],
) -> list[dict]:
    """Facturas agregadas que califican como compra de inventario en el dashboard."""
    fact_por_num: dict[str, dict] = {}
    for f in facturas:
        num = (f.get("num_factura") or "").strip()
        if num:
            fact_por_num[num] = f

    agg: dict[str, dict] = {}
    for r in rows:
        if not _es_compra_inventario_dashboard(
            r, mps_validos=mps_validos, prov_inv=prov_inv, fact_por_num=fact_por_num
        ):
            continue
        num = (r.get("num_documento") or "").strip() or "?"
        fecha = (str(r.get("fecha") or ""))[:10]
        ruc = _ruc_desde_mov(r, fact_por_num)
        prov = _proveedor_dashboard(r, fact_por_num, prov_inv) or ""
        ct = _to_float(r.get("costo_total"))
        bod = normalizar_cod_bodega(r.get("cod_bodega_destino") or r.get("cod_bodega_origen"))
        if num not in agg:
            agg[num] = {
                "num_factura": num,
                "fecha": fecha,
                "proveedor": prov,
                "area": _area_bodega(bod),
                "total": 0.0,
                "lineas": 0,
            }
        agg[num]["total"] += ct
        agg[num]["lineas"] += 1
        if fecha < agg[num]["fecha"]:
            agg[num]["fecha"] = fecha
    out = sorted(agg.values(), key=lambda x: (x["fecha"], x["num_factura"]))
    for row in out:
        row["total"] = round(row["total"], 2)
    return out


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
    prov_inv = _mapa_proveedores_inventario(rows_prov)

    rows = query_mov_fn(desde.isoformat(), hasta.isoformat())
    facturas = query_facturas_fn(desde.isoformat(), hasta.isoformat())
    fill_labels = _etiquetas_periodo(desde, hasta, agrup)
    actual = _metricas_compras(
        rows, facturas, prov_inv, mps_validos, mp_area, agrup=agrup, area_filtro=area, fill_labels=fill_labels
    )

    ini_a, fin_a = periodo_anterior(desde, hasta)
    ant = _metricas_compras(
        query_mov_fn(ini_a.isoformat(), fin_a.isoformat()),
        facturas,
        prov_inv,
        mps_validos,
        mp_area,
        agrup=agrup,
        area_filtro=area,
    )

    ini_y, fin_y = mismo_periodo_anio_anterior(desde, hasta)
    ya = _metricas_compras(
        query_mov_fn(ini_y.isoformat(), fin_y.isoformat()),
        facturas,
        prov_inv,
        mps_validos,
        mp_area,
        agrup=agrup,
        area_filtro=area,
    )

    ini_ytd, fin_ytd = acumulado_anio(hasta)
    ytd = _metricas_compras(
        query_mov_fn(ini_ytd.isoformat(), fin_ytd.isoformat()),
        facturas,
        prov_inv,
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
        "nota": (
            "Solo inventario: ENTRADA/ENTRADA_COSTO_HIST con MP en BD_MP_SISTEMA, "
            "bodegas 001/002/003/005. Excluye MP 000 y líneas sin catálogo. "
            "Proveedor desde BD_PROV o razón social de la factura."
        ),
    }

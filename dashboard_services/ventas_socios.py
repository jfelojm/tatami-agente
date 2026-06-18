"""Resumen ejecutivo de ventas para vista Socios."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from dashboard_services.periodos import (
    acumulado_anio,
    mismo_periodo_anio_anterior,
    periodo_anterior,
    resumen_comparativo,
)


def _metricas_periodo(rows: list[dict], catalogo: dict, resolver, neto_fn, dia_semana_fn) -> dict:
    desglose_pv: dict[str, dict] = {
        pv: {"vta": 0.0, "uds": 0.0} for pv in ("BARRA", "COCINA", "OTRO")
    }
    categorias: dict[str, float] = defaultdict(float)
    platos: dict[str, dict] = {}
    por_dia: dict[int, dict[str, float]] = {
        i: {"BARRA": 0.0, "COCINA": 0.0, "OTRO": 0.0} for i in range(1, 8)
    }
    lineas = 0
    lineas_otro = 0
    descuentos = 0.0
    subtotal_bruto = 0.0

    for r in rows:
        fecha = (r.get("fecha") or "")[:10]
        if not fecha:
            continue
        meta = resolver(
            catalogo,
            cod_smart_menu=r.get("cod_smart_menu") or "",
            variedad_smart_menu=r.get("variedad_smart_menu") or "",
            nombre_producto=r.get("nombre_producto") or "",
        )
        pv = meta["pv"]
        total = neto_fn(r)
        uds = float(r.get("cantidad_vendida") or 0)
        desc = float(r.get("descuento_valor") or 0)
        sub = float(r.get("subtotal") or 0)

        lineas += 1
        if pv == "OTRO":
            lineas_otro += 1
        descuentos += desc
        subtotal_bruto += sub

        desglose_pv[pv]["vta"] += total
        desglose_pv[pv]["uds"] += uds
        categorias[meta["cat"]] += total
        por_dia[dia_semana_fn(fecha)][pv] += total

        pk = f"{meta['cod_smart_menu']}|{meta['variedad_smart_menu']}|{meta['nombre']}"
        if pk not in platos:
            platos[pk] = {
                "nombre": meta["nombre"],
                "cat": meta["cat"],
                "pv": pv,
                "vta": 0.0,
                "uds": 0.0,
            }
        platos[pk]["vta"] += total
        platos[pk]["uds"] += uds

    vta = sum(d["vta"] for d in desglose_pv.values())
    uds = sum(d["uds"] for d in desglose_pv.values())
    ticket = vta / uds if uds else 0.0

    top_cats = sorted(
        [{"nombre": k, "vta": round(v, 2), "pct": round(v / vta * 100, 1) if vta else 0} for k, v in categorias.items()],
        key=lambda x: x["vta"],
        reverse=True,
    )[:10]
    top_platos = sorted(
        [
            {
                "nombre": p["nombre"],
                "cat": p["cat"],
                "pv": p["pv"],
                "vta": round(p["vta"], 2),
                "uds": round(p["uds"], 0),
            }
            for p in platos.values()
        ],
        key=lambda x: x["vta"],
        reverse=True,
    )[:10]

    dias_nombres = ["", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    por_dia_out = [
        {
            "dia": i,
            "nombre": dias_nombres[i],
            "vta": round(sum(por_dia[i].values()), 2),
            "barra": round(por_dia[i]["BARRA"], 2),
            "cocina": round(por_dia[i]["COCINA"], 2),
            "otro": round(por_dia[i]["OTRO"], 2),
        }
        for i in range(1, 8)
    ]
    finde = sum(sum(por_dia[d].values()) for d in (5, 6, 7))
    semana = sum(sum(por_dia[d].values()) for d in range(1, 8))
    mix_pv = {
        pv: {
            "vta": round(desglose_pv[pv]["vta"], 2),
            "uds": round(desglose_pv[pv]["uds"], 0),
            "pct": round(desglose_pv[pv]["vta"] / vta * 100, 1) if vta else 0,
        }
        for pv in ("BARRA", "COCINA", "OTRO")
    }

    return {
        "vta": round(vta, 2),
        "uds": round(uds, 0),
        "ticket": round(ticket, 2),
        "descuentos": round(descuentos, 2),
        "subtotal_bruto": round(subtotal_bruto, 2),
        "lineas": lineas,
        "lineas_otro": lineas_otro,
        "pct_otro": round(lineas_otro / lineas * 100, 1) if lineas else 0,
        "mix_pv": mix_pv,
        "top_categorias": top_cats,
        "top_platos": top_platos,
        "por_dia_semana": por_dia_out,
        "finde_pct": round(finde / semana * 100, 1) if semana else 0,
    }


def build_resumen_socios(
    *,
    query_fn,
    catalogo: dict,
    resolver,
    neto_fn,
    dia_semana_fn,
    desde: date,
    hasta: date,
) -> dict:
    """Construye payload completo vista Socios con comparativos."""

    def load(ini: date, fin: date) -> list[dict]:
        return query_fn(ini.isoformat(), fin.isoformat())

    actual_rows = load(desde, hasta)
    actual = _metricas_periodo(actual_rows, catalogo, resolver, neto_fn, dia_semana_fn)

    ini_ant, fin_ant = periodo_anterior(desde, hasta)
    ant_rows = load(ini_ant, fin_ant)
    anterior = _metricas_periodo(ant_rows, catalogo, resolver, neto_fn, dia_semana_fn)

    ini_y, fin_y = mismo_periodo_anio_anterior(desde, hasta)
    y_rows = load(ini_y, fin_y)
    anio_ant = _metricas_periodo(y_rows, catalogo, resolver, neto_fn, dia_semana_fn)

    ini_ytd, fin_ytd = acumulado_anio(hasta)
    ytd_rows = load(ini_ytd, fin_ytd)
    ytd = _metricas_periodo(ytd_rows, catalogo, resolver, neto_fn, dia_semana_fn)

    return {
        "periodo": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "actual": actual,
        "comparativo_anterior": {
            "periodo": {"desde": ini_ant.isoformat(), "hasta": fin_ant.isoformat()},
            "metricas": resumen_comparativo(actual, anterior),
        },
        "comparativo_anio": {
            "periodo": {"desde": ini_y.isoformat(), "hasta": fin_y.isoformat()},
            "metricas": resumen_comparativo(actual, anio_ant),
        },
        "acumulado_anio": {
            "periodo": {"desde": ini_ytd.isoformat(), "hasta": fin_ytd.isoformat()},
            "vta": ytd["vta"],
            "uds": ytd["uds"],
            "ticket": ytd["ticket"],
        },
    }

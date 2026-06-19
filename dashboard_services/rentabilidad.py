"""Rentabilidad bruta: ventas vs costo teórico y costo real (promedio período)."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date

from dashboard_services.periodos import acumulado_anio, periodo_anterior, resumen_comparativo
from matching_productos import cargar_bd_productos
from recetas_detalle import agrupar_por_plato, cargar_bd_recetas_detalle, clave_plato

_RENT_CACHE_TTL_SEC = 300
_recetas_por_plato_cache: dict[str, list] | None = None
_recetas_cache_at: float = 0.0
_costo_sub_cache: dict[str, float] | None = None
_costo_sub_cache_at: float = 0.0


def _recetas_por_plato_cached() -> dict[str, list]:
    global _recetas_por_plato_cache, _recetas_cache_at
    now = time.monotonic()
    if _recetas_por_plato_cache is not None and (now - _recetas_cache_at) < _RENT_CACHE_TTL_SEC:
        return _recetas_por_plato_cache
    recetas = cargar_bd_recetas_detalle()
    _recetas_por_plato_cache = agrupar_por_plato(recetas)
    _recetas_cache_at = now
    return _recetas_por_plato_cache


def _costo_sub_cached() -> dict[str, float]:
    global _costo_sub_cache, _costo_sub_cache_at
    now = time.monotonic()
    if _costo_sub_cache is not None and (now - _costo_sub_cache_at) < _RENT_CACHE_TTL_SEC:
        return _costo_sub_cache
    try:
        from calcular_costo_subrecetas import calcular_costos, cargar_contexto_subrecetas

        cab, por_padre, costos_mp, _ = cargar_contexto_subrecetas()
        resultados, _ = calcular_costos(cab, por_padre, costos_mp)
        _costo_sub_cache = {
            k: v.get("costo_unitario_estandar") or v.get("costo_unitario") or 0
            for k, v in resultados.items()
        }
    except Exception as e:
        print(f"WARN costo_sub rentabilidad: {e}")
        _costo_sub_cache = {}
    _costo_sub_cache_at = now
    return _costo_sub_cache


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or default)
    except (TypeError, ValueError):
        return default


def costo_promedio_mp_periodo(rows_entrada: list[dict]) -> dict[str, float]:
    acc: dict[str, list[float]] = defaultdict(list)
    for r in rows_entrada:
        tipo = (r.get("tipo_mov") or "").strip()
        if tipo not in ("ENTRADA", "ENTRADA_COSTO_HIST"):
            continue
        cod = (r.get("cod_mp_sistema") or "").strip()
        cu = _to_float(r.get("costo_unitario"))
        if cod and cu > 0:
            acc[cod].append(cu)
    return {k: round(sum(v) / len(v), 6) for k, v in acc.items() if v}


def _costo_plato_receta(
    cod_receta: str,
    variedad: str,
    recetas_por_plato: dict,
    costo_mp: dict[str, float],
    costo_sub: dict[str, float],
) -> tuple[float, str]:
    lineas = recetas_por_plato.get(clave_plato(cod_receta.strip(), variedad or ""), [])
    if not lineas and variedad:
        lineas = recetas_por_plato.get(clave_plato(cod_receta.strip(), ""), [])
    if not lineas:
        return 0.0, "sin_receta"
    total = 0.0
    sin = 0
    for ln in lineas:
        tipo = (ln.get("tipo_linea") or ln.get("tipo") or "").strip().upper()
        cant = _to_float(ln.get("cantidad"))
        pct = _to_float(ln.get("pct_aplicacion"), 1.0) or 1.0
        merma = _to_float(ln.get("merma_pct"))
        if tipo == "SUB" or (ln.get("cod_subreceta") or "").strip():
            sub = (ln.get("cod_subreceta") or "").strip()
            cu = costo_sub.get(sub, 0.0)
            if cu <= 0:
                sin += 1
            total += cant * cu * pct
        else:
            mp = (ln.get("cod_mp_sistema") or "").strip()
            cu = costo_mp.get(mp)
            if cu is None or cu <= 0:
                try:
                    from calcular_costo_subrecetas import _costo_mp as fallback_mp_cost

                    cu = fallback_mp_cost(mp, ln)
                except Exception:
                    cu = 0.0
            if cu <= 0:
                sin += 1
            total += cant * cu * (1 + merma) * pct
    nota = "ok" if sin == 0 else f"{sin}_lineas_sin_costo"
    return round(total, 4), nota


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


def costo_compras_periodo(rows_entrada: list[dict]) -> float:
    """Suma costo_total inventario (excluye MP 000 y sin catálogo)."""
    from procesar_facturas_drive import COD_MP_SIN_CLASIFICAR

    total = 0.0
    for r in rows_entrada:
        tipo = (r.get("tipo_mov") or "").strip()
        if tipo not in ("ENTRADA", "ENTRADA_COSTO_HIST"):
            continue
        cod = (r.get("cod_mp_sistema") or "").strip()
        if not cod or cod == COD_MP_SIN_CLASIFICAR:
            continue
        obs = (r.get("observaciones") or "").upper()
        if "APPROX_SIN_CATALOGO" in obs:
            continue
        ct = _to_float(r.get("costo_total"))
        if ct > 0:
            total += ct
    return round(total, 2)


def costo_mp_por_buckets(rows_entrada: list[dict], agrup: str) -> dict[str, dict[str, float]]:
    """Promedio de costo MP por bucket temporal (mes/semana/día)."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows_entrada:
        fecha = (r.get("fecha") or "")[:10]
        if not fecha:
            continue
        key = _clave_agrup(fecha, agrup)
        grouped[key].append(r)
    return {k: costo_promedio_mp_periodo(v) for k, v in grouped.items()}


def build_rentabilidad_from_catalog(
    *,
    rows_ventas: list[dict],
    rows_entrada: list[dict],
    catalogo: dict,
    resolver,
    neto_fn,
    desde: date,
    hasta: date,
    agrup: str = "mes",
) -> dict:
    recetas_por_plato = _recetas_por_plato_cached()
    costo_mp_global = costo_promedio_mp_periodo(rows_entrada)
    costo_mp_buckets = costo_mp_por_buckets(rows_entrada, agrup)
    costo_sub = _costo_sub_cached()

    platos: dict[str, dict] = {}
    costo_cache: dict[str, dict[str, float]] = {}
    serie: dict[str, dict[str, float]] = defaultdict(
        lambda: {"vta": 0.0, "costo": 0.0, "BARRA": 0.0, "COCINA": 0.0, "OTRO": 0.0}
    )
    desglose_pv: dict[str, float] = {"BARRA": 0.0, "COCINA": 0.0, "OTRO": 0.0}
    margen_pv: dict[str, float] = {"BARRA": 0.0, "COCINA": 0.0, "OTRO": 0.0}

    def unit_cost(cod_rec: str, var: str, costo_mp: dict[str, float]) -> float:
        cache_key = id(costo_mp)
        bucket_cache = costo_cache.setdefault(str(cache_key), {})
        k = f"{cod_rec}|{var}"
        if k in bucket_cache:
            return bucket_cache[k]
        ct, _ = _costo_plato_receta(cod_rec, var, recetas_por_plato, costo_mp, costo_sub)
        if ct <= 0 and cod_rec:
            ct, _ = _costo_plato_receta(cod_rec, "", recetas_por_plato, costo_mp, costo_sub)
        bucket_cache[k] = ct
        return ct

    for r in rows_ventas:
        meta = resolver(
            catalogo,
            cod_smart_menu=r.get("cod_smart_menu") or "",
            variedad_smart_menu=r.get("variedad_smart_menu") or "",
            nombre_producto=r.get("nombre_producto") or "",
        )
        cod_rec = (r.get("cod_receta") or meta.get("cod_receta") or "").strip()
        uds = _to_float(r.get("cantidad_vendida"))
        vta = neto_fn(r)
        pv = meta.get("pv", "OTRO")
        fecha = (r.get("fecha") or "")[:10]
        bucket_key = _clave_agrup(fecha, agrup) if fecha else ""
        costo_mp = costo_mp_buckets.get(bucket_key) or costo_mp_global
        cu = unit_cost(cod_rec, meta.get("variedad_smart_menu", ""), costo_mp)
        costo = cu * uds
        margen = vta - costo

        if fecha:
            serie[bucket_key]["vta"] += vta
            serie[bucket_key]["costo"] += costo
            serie[bucket_key][pv] = serie[bucket_key].get(pv, 0.0) + margen

        desglose_pv[pv] = desglose_pv.get(pv, 0.0) + vta
        margen_pv[pv] = margen_pv.get(pv, 0.0) + margen

        pk = f"{meta.get('cod_smart_menu')}|{meta.get('variedad_smart_menu')}|{meta.get('nombre')}"
        if pk not in platos:
            platos[pk] = {
                "nombre": meta.get("nombre", pk),
                "pv": pv,
                "cat": meta.get("cat", ""),
                "vta": 0.0,
                "uds": 0.0,
                "costo_acum": 0.0,
            }
        platos[pk]["vta"] += vta
        platos[pk]["uds"] += uds
        platos[pk]["costo_acum"] += costo

    out_platos = []
    vta_total = 0.0
    costo_t_total = 0.0
    costo_r_total = 0.0
    for p in platos.values():
        uds = p["uds"] or 0
        ct = p["costo_acum"]
        cr = ct
        vta = p["vta"]
        vta_total += vta
        costo_t_total += ct
        costo_r_total += cr
        margen_t = vta - ct
        margen_r = vta - cr
        cu_ref = round(ct / uds, 4) if uds else 0.0
        out_platos.append(
            {
                "nombre": p["nombre"],
                "pv": p["pv"],
                "cat": p["cat"],
                "vta": round(vta, 2),
                "uds": round(uds, 0),
                "costo_teorico": round(ct, 2),
                "costo_real": round(cr, 2),
                "costo_unit_teorico": cu_ref,
                "costo_unit_real": cu_ref,
                "margen_teorico": round(margen_t, 2),
                "margen_real": round(margen_r, 2),
                "margen_teorico_pct": round(margen_t / vta * 100, 1) if vta else 0,
                "margen_real_pct": round(margen_r / vta * 100, 1) if vta else 0,
            }
        )

    out_platos.sort(key=lambda x: x["vta"], reverse=True)
    margen_t_pct = round((vta_total - costo_t_total) / vta_total * 100, 1) if vta_total else 0
    margen_r_pct = round((vta_total - costo_r_total) / vta_total * 100, 1) if vta_total else 0
    costo_compras = costo_compras_periodo(rows_entrada)
    margen_compras = round(vta_total - costo_compras, 2)
    margen_compras_pct = round(margen_compras / vta_total * 100, 1) if vta_total else 0

    ini_a, fin_a = periodo_anterior(desde, hasta)
    _ = ini_a, fin_a
    labels = sorted(serie.keys())
    margen_serie = [round(serie[k]["vta"] - serie[k]["costo"], 2) for k in labels]
    margen_pct_serie = [
        round((serie[k]["vta"] - serie[k]["costo"]) / serie[k]["vta"] * 100, 1)
        if serie[k]["vta"]
        else 0.0
        for k in labels
    ]
    vta_serie = [round(serie[k]["vta"], 2) for k in labels]
    barra_serie = [round(serie[k].get("BARRA", 0), 2) for k in labels]
    cocina_serie = [round(serie[k].get("COCINA", 0), 2) for k in labels]
    otro_serie = [round(serie[k].get("OTRO", 0), 2) for k in labels]

    def _pct_pv(margen_vals: list[float]) -> list[float]:
        out: list[float] = []
        for i, m in enumerate(margen_vals):
            vta_b = serie[labels[i]]["vta"]
            out.append(round(m / vta_b * 100, 1) if vta_b else 0.0)
        return out

    return {
        "periodo": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "agrup": agrup,
        "labels": labels,
        "margen": margen_serie,
        "margen_pct": margen_pct_serie,
        "vta_serie": vta_serie,
        "barra": barra_serie,
        "cocina": cocina_serie,
        "otro": otro_serie,
        "barra_pct": _pct_pv(barra_serie),
        "cocina_pct": _pct_pv(cocina_serie),
        "otro_pct": _pct_pv(otro_serie),
        "desglose_pv": {k: round(v, 2) for k, v in desglose_pv.items()},
        "margen_pv": {k: round(v, 2) for k, v in margen_pv.items()},
        "resumen": {
            "vta": round(vta_total, 2),
            "costo_teorico": round(costo_t_total, 2),
            "costo_real": round(costo_r_total, 2),
            "costo_compras_periodo": costo_compras,
            "margen_teorico": round(vta_total - costo_t_total, 2),
            "margen_real": round(vta_total - costo_r_total, 2),
            "margen_compras": margen_compras,
            "margen_teorico_pct": margen_t_pct,
            "margen_real_pct": margen_r_pct,
            "margen_compras_pct": margen_compras_pct,
        },
        "platos": out_platos[:50],
        "nota_costo": (
            "Margen por plato usa costo promedio ENTRADA/ENTRADA_COSTO_HIST por MP. "
            "margen_compras = ventas − Σ compras del período (incluye líneas aproximadas sin catálogo)."
        ),
    }

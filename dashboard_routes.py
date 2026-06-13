"""Rutas HTTP para dashboards de ventas (HTML + JSON)."""

from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from supabase import create_client

from dashboard_services.compras import build_resumen_compras
from dashboard_services.confianza import build_confianza_inventario
from dashboard_services.inventario_vivo import build_inventario_vivo
from dashboard_services.meta import meta_dashboard
from dashboard_services.rentabilidad import build_rentabilidad_from_catalog
from dashboard_services.roturas import build_roturas_historico
from dashboard_services.sheets_data import leer_bd_mp_sistema, leer_bd_prov
from dashboard_services.ventas_socios import build_resumen_socios
from matching_productos import cargar_bd_productos
from ventas_smartmenu import estado_documento_excluye_neto_operativo

router = APIRouter()

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "tatami2026")
_DASH_DIR = Path(__file__).resolve().parent
_DASHBOARD_HTML = _DASH_DIR / "dashboard.html"


def _escape_script(js: str) -> str:
    return js.replace("</script>", "<\\/script>")


def _inline_dashboard_js(html: str) -> str:
    """Incrusta JS si aún hay tags externos (dev local con archivos .js separados)."""
    if "/dashboard-filters.js" not in html and "/dashboard-app.js" not in html:
        return html
    pairs = (
        ("dashboard_filters.js", "/dashboard-filters.js"),
        ("dashboard_app.js", "/dashboard-app.js"),
    )
    for filename, src_tag in pairs:
        js_path = _DASH_DIR / filename
        src = f'<script src="{src_tag}"></script>'
        if src not in html or not js_path.is_file():
            continue
        js = _escape_script(js_path.read_text(encoding="utf-8"))
        html = html.replace(src, f"<script>\n{js}\n</script>")
    return html
_cache_catalogo: dict | None = None


def _check_token(token: str) -> None:
    if token != DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="No autorizado")


def _get_sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).upper()


def _col(row: dict, *names: str) -> str:
    """Lee columna de BD_PRODUCTOS sin depender de mayúsculas."""
    if not row:
        return ""
    norm = {_norm_key(k): v for k, v in row.items()}
    for name in names:
        v = norm.get(_norm_key(name))
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


def _norm_punto_venta(raw: str) -> str:
    pv = _norm_key(raw)
    if pv in ("BARRA", "BAR"):
        return "BARRA"
    if pv in ("COCINA", "COCIN"):
        return "COCINA"
    if pv in ("OTRO", "OTROS", ""):
        return "OTRO"
    return pv or "OTRO"


def _cargar_catalogo() -> dict:
    global _cache_catalogo
    if _cache_catalogo is not None:
        return _cache_catalogo

    by_cod_var: dict[tuple[str, str], dict] = {}
    by_cod: dict[str, list[dict]] = defaultdict(list)
    variedades_por_cod: dict[str, int] = defaultdict(int)

    for p in cargar_bd_productos():
        cod = _col(p, "cod_smart_menu")
        if not cod:
            continue
        var = _col(p, "variedad_smart_menu")
        meta = {
            "pv": _norm_punto_venta(_col(p, "punto_venta", "Punto_venta")),
            "cat": _col(p, "categoria_menu", "Categoría") or "SIN CATEGORIA",
            "nombre": _col(p, "nombre_producto") or cod,
            "cod_smart_menu": cod,
            "variedad_smart_menu": var,
            "cod_receta": _col(p, "cod_receta", "Cod_receta"),
        }
        by_cod_var[(cod, var)] = meta
        by_cod[cod].append(meta)
        if var:
            variedades_por_cod[cod] += 1

    multivariety_cods = {c for c, n in variedades_por_cod.items() if n > 1}
    _cache_catalogo = {
        "by_cod_var": by_cod_var,
        "by_cod": dict(by_cod),
        "multivariety_cods": multivariety_cods,
    }
    return _cache_catalogo


def _nombre_display(meta: dict, multivariety_cods: set[str]) -> str:
    base = (meta.get("nombre") or meta.get("cod_smart_menu") or "").strip()
    var = (meta.get("variedad_smart_menu") or "").strip()
    cod = (meta.get("cod_smart_menu") or "").strip()
    if var and (cod in multivariety_cods or _norm_key(var) != _norm_key(base)):
        return f"{base} — {var}"
    return base


def _plato_out(raw: dict, multivariety_cods: set[str]) -> dict:
    meta = {
        "nombre": raw.get("nombre", ""),
        "cod_smart_menu": raw.get("cod_smart_menu", ""),
        "variedad_smart_menu": raw.get("variedad_smart_menu", ""),
    }
    cod = (meta["cod_smart_menu"] or "").strip()
    return {
        "nombre": meta["nombre"],
        "nombre_display": _nombre_display(meta, multivariety_cods),
        "cod_smart_menu": cod,
        "variedad_smart_menu": meta["variedad_smart_menu"],
        "tiene_variedades": cod in multivariety_cods,
        "vta": round(float(raw.get("vta") or 0), 2),
        "uds": round(float(raw.get("uds") or 0), 0),
    }


def _agrupar_productos_categoria(
    platos: list[dict], multivariety_cods: set[str], orden: str
) -> list[dict]:
    """Agrupa platos con variedades bajo un producto padre (ej. BAO)."""
    singles: list[dict] = []
    by_cod: dict[str, list[dict]] = defaultdict(list)
    for p in platos:
        cod = (p.get("cod_smart_menu") or "").strip()
        if cod and cod in multivariety_cods:
            by_cod[cod].append(p)
        else:
            singles.append({**p, "tipo": "plato"})

    out = list(singles)
    for cod, vars_list in by_cod.items():
        if len(vars_list) == 1:
            out.append({**vars_list[0], "tipo": "plato"})
            continue
        tv = sum(x["vta"] for x in vars_list)
        tu = sum(x["uds"] for x in vars_list)
        out.append(
            {
                "tipo": "producto",
                "nombre": vars_list[0]["nombre"],
                "nombre_display": vars_list[0]["nombre"],
                "cod_smart_menu": cod,
                "tiene_variedades": True,
                "vta": round(tv, 2),
                "uds": round(tu, 0),
                "variedades": _ordenar_items(
                    [{**v, "tipo": "variedad"} for v in vars_list], orden
                ),
            }
        )
    return _ordenar_items(out, orden)


def _resolver_producto(
    catalogo: dict,
    *,
    cod_smart_menu: str,
    variedad_smart_menu: str,
    nombre_producto: str,
) -> dict:
    csm = (cod_smart_menu or "").strip()
    vsm = (variedad_smart_menu or "").strip()
    nombre_hist = (nombre_producto or "").strip()
    by_cod_var: dict[tuple[str, str], dict] = catalogo["by_cod_var"]
    by_cod: dict[str, list[dict]] = catalogo["by_cod"]

    if not csm:
        return {
            "pv": "OTRO",
            "cat": "SIN CATEGORIA",
            "nombre": nombre_hist or "(sin nombre)",
            "cod_smart_menu": "",
            "variedad_smart_menu": vsm,
        }

    if (csm, vsm) in by_cod_var:
        return by_cod_var[(csm, vsm)]

    vsm_u = _norm_key(vsm)
    for (c, v), meta in by_cod_var.items():
        if c == csm and _norm_key(v) == vsm_u:
            return meta

    if vsm_u:
        for (c, v), meta in by_cod_var.items():
            if c == csm and v and _norm_key(v) in vsm_u:
                return meta

    filas = by_cod.get(csm, [])
    if len(filas) == 1:
        return filas[0]

    return {
        "pv": "OTRO",
        "cat": "SIN CATEGORIA",
        "nombre": nombre_hist or csm,
        "cod_smart_menu": csm,
        "variedad_smart_menu": vsm,
    }


def _sanitize_fecha(fecha: str | None) -> str | None:
    """Normaliza YYYY-MM-DD; corrige días inválidos (ej. 2026-06-31)."""
    if not fecha:
        return None
    raw = str(fecha).strip()[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        parts = raw.split("-")
        if len(parts) != 3:
            raise HTTPException(status_code=400, detail=f"Fecha inválida: {fecha}")
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        if m < 1 or m > 12:
            raise HTTPException(status_code=400, detail=f"Fecha inválida: {fecha}")
        from calendar import monthrange

        last = monthrange(y, m)[1]
        return date(y, m, min(max(d, 1), last)).isoformat()


def _query_hist_ventas(
    sb,
    *,
    desde: str | None,
    hasta: str | None,
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = sb.table("hist_ventas").select(
            "fecha,cod_smart_menu,variedad_smart_menu,nombre_producto,cod_receta,"
            "cantidad_vendida,subtotal,descuento_valor,estado_documento"
        )
        if desde:
            q = q.gte("fecha", desde)
        if hasta:
            q = q.lte("fecha", hasta)
        chunk = q.range(offset, offset + 999).execute().data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return [
        r
        for r in rows
        if not estado_documento_excluye_neto_operativo(r.get("estado_documento"))
    ]


def _neto_linea(row: dict) -> float:
    subtotal = float(row.get("subtotal") or 0)
    descuento = float(row.get("descuento_valor") or 0)
    return subtotal - descuento


def _clave_agrupacion(fecha: str, agrup: str) -> str:
    d = date.fromisoformat(fecha[:10])
    if agrup == "dia":
        return fecha[:10]
    if agrup == "semana":
        return d.strftime("%G-W%V")
    if agrup == "anio":
        return fecha[:4]
    return fecha[:7]


def _dia_semana_iso(fecha: str) -> int:
    """1=lunes ... 7=domingo (como el selector del dashboard)."""
    return date.fromisoformat(fecha[:10]).isoweekday()


def _match_filtro_plato(meta: dict, plato: str) -> bool:
    if not plato:
        return True
    plato = plato.strip()
    if "|" in plato:
        cod, var = plato.split("|", 1)
        return (
            meta.get("cod_smart_menu", "") == cod.strip()
            and meta.get("variedad_smart_menu", "") == var.strip()
        )
    return _norm_key(meta.get("nombre", "")) == _norm_key(plato)


def _ordenar_items(items: list[dict], orden: str, key_vta: str = "vta", key_uds: str = "uds") -> list[dict]:
    rev = (orden or "desc").lower() != "asc"
    return sorted(items, key=lambda x: (x.get(key_vta, 0), x.get(key_uds, 0)), reverse=rev)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(token: str = ""):
    _check_token(token)
    if not _DASHBOARD_HTML.is_file():
        raise HTTPException(
            status_code=503,
            detail="dashboard.html no existe aún. Crear el archivo en tatami-agente/.",
        )
    html = _DASHBOARD_HTML.read_text(encoding="utf-8")
    html = html.replace("__TOKEN__", token)
    html = _inline_dashboard_js(html)
    return HTMLResponse(content=html)


@router.get("/dashboard-app.js")
def dashboard_app_js():
    js_path = Path(__file__).resolve().parent / "dashboard_app.js"
    if not js_path.is_file():
        raise HTTPException(status_code=404, detail="dashboard_app.js no encontrado")
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(js_path.read_text(encoding="utf-8"), media_type="application/javascript")


@router.get("/dashboard-filters.js")
def dashboard_filters_js():
    js_path = Path(__file__).resolve().parent / "dashboard_filters.js"
    if not js_path.is_file():
        raise HTTPException(status_code=404, detail="dashboard_filters.js no encontrado")
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(js_path.read_text(encoding="utf-8"), media_type="application/javascript")


@router.get("/api/dashboard/ventas")
def ventas(
    token: str = "",
    desde: str | None = Query(default=None),
    hasta: str | None = Query(default=None),
    agrup: str = Query(default="mes"),
    dia_semana: int | None = Query(default=None, ge=1, le=7),
    punto_venta: list[str] | None = Query(default=None),
    categoria: str | None = Query(default=None),
    plato: str | None = Query(default=None),
    orden: str = Query(default="desc"),
    incluir_socios: bool = Query(default=False),
):
    _check_token(token)
    sb = _get_sb()
    catalogo = _cargar_catalogo()
    multivariety_cods: set[str] = catalogo["multivariety_cods"]
    desde = _sanitize_fecha(desde)
    hasta = _sanitize_fecha(hasta)
    if desde and hasta and desde > hasta:
        raise HTTPException(status_code=400, detail="desde no puede ser posterior a hasta")
    rows = _query_hist_ventas(sb, desde=desde, hasta=hasta)

    pv_filtros: set[str] | None = None
    if punto_venta:
        pv_filtros = set()
        for raw in punto_venta:
            for part in raw.split(","):
                pv_norm = _norm_punto_venta(part.strip())
                if pv_norm:
                    pv_filtros.add(pv_norm)
        if not pv_filtros:
            pv_filtros = None
    cat_filtro = _norm_key(categoria) if categoria else ""

    resumen: dict[str, dict[str, float]] = defaultdict(
        lambda: {"BARRA": 0.0, "COCINA": 0.0, "OTRO": 0.0}
    )
    plato_trend: dict[str, dict[str, float]] = defaultdict(lambda: {"vta": 0.0, "uds": 0.0})
    desglose: dict[str, dict] = {
        pv: {"vta": 0.0, "uds": 0.0, "categorias": defaultdict(lambda: {"vta": 0.0, "uds": 0.0, "platos": {}})}
        for pv in ("BARRA", "COCINA", "OTRO")
    }
    categorias_flat: dict[str, dict] = defaultdict(lambda: {"vta": 0.0, "uds": 0.0, "pv": set()})
    platos_flat: dict[str, dict] = {}
    productos_flat: dict[str, dict] = {}

    for r in rows:
        fecha = (r.get("fecha") or "")[:10]
        if not fecha:
            continue
        if dia_semana and _dia_semana_iso(fecha) != dia_semana:
            continue

        meta = _resolver_producto(
            catalogo,
            cod_smart_menu=r.get("cod_smart_menu") or "",
            variedad_smart_menu=r.get("variedad_smart_menu") or "",
            nombre_producto=r.get("nombre_producto") or "",
        )
        pv = meta["pv"]
        cat = meta["cat"]
        nombre = meta["nombre"]

        if pv_filtros and pv not in pv_filtros:
            continue
        if cat_filtro and _norm_key(cat) != cat_filtro:
            continue
        if plato and not _match_filtro_plato(meta, plato):
            continue

        total = _neto_linea(r)
        uds = float(r.get("cantidad_vendida") or 0)
        key = _clave_agrupacion(fecha, agrup)

        resumen[key][pv] += total
        desglose[pv]["vta"] += total
        desglose[pv]["uds"] += uds

        cat_bucket = desglose[pv]["categorias"][cat]
        cat_bucket["vta"] += total
        cat_bucket["uds"] += uds

        plato_key = f"{meta['cod_smart_menu']}|{meta['variedad_smart_menu']}|{nombre}"
        if plato_key not in cat_bucket["platos"]:
            cat_bucket["platos"][plato_key] = {
                "nombre": nombre,
                "cod_smart_menu": meta["cod_smart_menu"],
                "variedad_smart_menu": meta["variedad_smart_menu"],
                "vta": 0.0,
                "uds": 0.0,
            }
        cat_bucket["platos"][plato_key]["vta"] += total
        cat_bucket["platos"][plato_key]["uds"] += uds

        categorias_flat[cat]["vta"] += total
        categorias_flat[cat]["uds"] += uds
        categorias_flat[cat]["pv"].add(pv)

        if plato_key not in platos_flat:
            platos_flat[plato_key] = {
                "nombre": nombre,
                "cod_smart_menu": meta["cod_smart_menu"],
                "variedad_smart_menu": meta["variedad_smart_menu"],
                "pv": pv,
                "cat": cat,
                "vta": 0.0,
                "uds": 0.0,
            }
        platos_flat[plato_key]["vta"] += total
        platos_flat[plato_key]["uds"] += uds

        cod_prod = (meta["cod_smart_menu"] or "").strip()
        if cod_prod:
            if cod_prod not in productos_flat:
                productos_flat[cod_prod] = {
                    "nombre": meta["nombre"],
                    "cod_smart_menu": cod_prod,
                    "pv": pv,
                    "cat": cat,
                    "vta": 0.0,
                    "uds": 0.0,
                    "variedades": {},
                }
            productos_flat[cod_prod]["vta"] += total
            productos_flat[cod_prod]["uds"] += uds
            var_key = (meta["variedad_smart_menu"] or "").strip() or "(sin variedad)"
            if var_key not in productos_flat[cod_prod]["variedades"]:
                productos_flat[cod_prod]["variedades"][var_key] = {
                    "variedad": var_key,
                    "nombre_display": _nombre_display(meta, multivariety_cods),
                    "cod_smart_menu": cod_prod,
                    "variedad_smart_menu": meta["variedad_smart_menu"],
                    "vta": 0.0,
                    "uds": 0.0,
                }
            productos_flat[cod_prod]["variedades"][var_key]["vta"] += total
            productos_flat[cod_prod]["variedades"][var_key]["uds"] += uds

        if plato:
            plato_trend[key]["vta"] += total
            plato_trend[key]["uds"] += uds

    labels = sorted(resumen.keys())
    desglose_out: dict[str, dict] = {}
    for pv in ("BARRA", "COCINA", "OTRO"):
        cats_out = []
        for cat, cdata in desglose[pv]["categorias"].items():
            platos_raw = [
                _plato_out(p, multivariety_cods) for p in cdata["platos"].values()
            ]
            platos_list = _ordenar_items(platos_raw, orden)
            cats_out.append(
                {
                    "nombre": cat,
                    "vta": round(cdata["vta"], 2),
                    "uds": round(cdata["uds"], 0),
                    "platos": platos_list,
                    "productos": _agrupar_productos_categoria(
                        platos_list, multivariety_cods, orden
                    ),
                }
            )
        cats_out = _ordenar_items(cats_out, orden)
        desglose_out[pv] = {
            "vta": round(desglose[pv]["vta"], 2),
            "uds": round(desglose[pv]["uds"], 0),
            "categorias": cats_out,
            "platos": _ordenar_items(
                [_plato_out(p, multivariety_cods) for c in cats_out for p in c["platos"]],
                orden,
            ),
        }

    categorias_list = _ordenar_items(
        [
            {
                "nombre": cat,
                "vta": round(d["vta"], 2),
                "uds": round(d["uds"], 0),
                "puntos_venta": sorted(d["pv"]),
            }
            for cat, d in categorias_flat.items()
        ],
        orden,
    )
    platos_list = _ordenar_items(
        [
            {
                **_plato_out(p, multivariety_cods),
                "pv": p["pv"],
                "cat": p["cat"],
                "id": f"{p['cod_smart_menu']}|{p['variedad_smart_menu']}",
            }
            for p in platos_flat.values()
        ],
        orden,
    )
    productos_list = _ordenar_items(
        [
            {
                "nombre": p["nombre"],
                "nombre_display": p["nombre"],
                "cod_smart_menu": p["cod_smart_menu"],
                "pv": p["pv"],
                "cat": p["cat"],
                "tiene_variedades": p["cod_smart_menu"] in multivariety_cods,
                "vta": round(p["vta"], 2),
                "uds": round(p["uds"], 0),
                "variedades": _ordenar_items(
                    [
                        {
                            "variedad": v["variedad"],
                            "nombre_display": v["nombre_display"],
                            "cod_smart_menu": v["cod_smart_menu"],
                            "variedad_smart_menu": v["variedad_smart_menu"],
                            "vta": round(v["vta"], 2),
                            "uds": round(v["uds"], 0),
                            "id": f"{v['cod_smart_menu']}|{v['variedad_smart_menu']}",
                        }
                        for v in p["variedades"].values()
                    ],
                    orden,
                ),
            }
            for p in productos_flat.values()
            if p["cod_smart_menu"] in multivariety_cods
        ],
        orden,
    )

    plato_labels = sorted(plato_trend.keys()) if plato else []
    plato_nombre = ""
    if plato:
        for p in platos_list:
            if _match_filtro_plato(p, plato):
                plato_nombre = p["nombre"]
                break

    result = {
        "labels": labels,
        "cocina": [round(resumen[k]["COCINA"], 2) for k in labels],
        "barra": [round(resumen[k]["BARRA"], 2) for k in labels],
        "otro": [round(resumen[k]["OTRO"], 2) for k in labels],
        "total_cocina": round(sum(v["COCINA"] for v in resumen.values()), 2),
        "total_barra": round(sum(v["BARRA"] for v in resumen.values()), 2),
        "total_otro": round(sum(v["OTRO"] for v in resumen.values()), 2),
        "desglose": desglose_out,
        "categorias": categorias_list,
        "platos": platos_list,
        "productos": productos_list,
        "top_barra": desglose_out["BARRA"]["platos"][:20],
        "top_cocina": desglose_out["COCINA"]["platos"][:20],
        "top_otro": desglose_out["OTRO"]["platos"][:10],
        "plato_trend": {
            "nombre": plato_nombre,
            "labels": plato_labels,
            "vta": [round(plato_trend[k]["vta"], 2) for k in plato_labels],
            "uds": [round(plato_trend[k]["uds"], 0) for k in plato_labels],
        }
        if plato
        else None,
    }
    if incluir_socios and desde and hasta and not plato:
        result["socios"] = build_resumen_socios(
            query_fn=lambda d, h: _query_hist_ventas(sb, desde=d, hasta=h),
            catalogo=catalogo,
            resolver=_resolver_producto,
            neto_fn=_neto_linea,
            dia_semana_fn=_dia_semana_iso,
            desde=date.fromisoformat(desde),
            hasta=date.fromisoformat(hasta),
        )
    return result


def _query_mov_inventario(sb, *, desde: str | None, hasta: str | None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = sb.table("mov_inventario").select(
            "fecha,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
            "costo_unitario,costo_total,num_documento,cod_bodega_origen,cod_bodega_destino,observaciones"
        )
        if desde:
            q = q.gte("fecha", desde)
        if hasta:
            q = q.lte("fecha", hasta + "T23:59:59")
        chunk = q.range(offset, offset + 999).execute().data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def _query_facturas_procesadas(sb, *, desde: str | None, hasta: str | None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = sb.table("facturas_procesadas").select(
            "num_factura,ruc_proveedor,fecha_factura,estado,meta"
        )
        if desde:
            q = q.gte("fecha_factura", desde)
        if hasta:
            q = q.lte("fecha_factura", hasta)
        chunk = q.range(offset, offset + 999).execute().data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    out = []
    for r in rows:
        meta = r.get("meta") or {}
        if isinstance(meta, dict):
            razon = meta.get("razon_social") or meta.get("proveedor") or ""
        else:
            razon = ""
        out.append({**r, "razon_social": razon})
    return out


def _query_conteo_ciclos(sb, limit: int = 50) -> list[dict]:
    return (
        sb.table("conteo_ciclo")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )


@router.get("/api/dashboard/meta")
def dashboard_meta(token: str = ""):
    _check_token(token)
    return meta_dashboard(_get_sb())


@router.get("/api/dashboard/ventas/socios")
def ventas_socios(
    token: str = "",
    desde: str | None = Query(default=None),
    hasta: str | None = Query(default=None),
):
    _check_token(token)
    sb = _get_sb()
    catalogo = _cargar_catalogo()
    desde_d = _sanitize_fecha(desde)
    hasta_d = _sanitize_fecha(hasta)
    if not desde_d or not hasta_d:
        raise HTTPException(status_code=400, detail="desde y hasta son obligatorios")
    if desde_d > hasta_d:
        raise HTTPException(status_code=400, detail="desde no puede ser posterior a hasta")

    def query_fn(d: str, h: str) -> list[dict]:
        return _query_hist_ventas(sb, desde=d, hasta=h)

    return build_resumen_socios(
        query_fn=query_fn,
        catalogo=catalogo,
        resolver=_resolver_producto,
        neto_fn=_neto_linea,
        dia_semana_fn=_dia_semana_iso,
        desde=date.fromisoformat(desde_d),
        hasta=date.fromisoformat(hasta_d),
    )


@router.get("/api/dashboard/compras")
def dashboard_compras(
    token: str = "",
    desde: str | None = Query(default=None),
    hasta: str | None = Query(default=None),
    agrup: str = Query(default="mes"),
    area: str | None = Query(default=None),
):
    _check_token(token)
    sb = _get_sb()
    desde = _sanitize_fecha(desde)
    hasta = _sanitize_fecha(hasta)
    if not desde or not hasta:
        raise HTTPException(status_code=400, detail="desde y hasta son obligatorios")
    try:
        rows_mp = leer_bd_mp_sistema()
        rows_prov = leer_bd_prov()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"No se pudo leer Sheets: {e}") from e
    return build_resumen_compras(
        query_mov_fn=lambda d, h: _query_mov_inventario(sb, desde=d, hasta=h),
        query_facturas_fn=lambda d, h: _query_facturas_procesadas(sb, desde=d, hasta=h),
        rows_mp=rows_mp,
        rows_prov=rows_prov,
        desde=date.fromisoformat(desde),
        hasta=date.fromisoformat(hasta),
        agrup=agrup,
        area=(area or "").upper() or None,
    )


@router.get("/api/dashboard/inventario/vivo")
def dashboard_inventario_vivo(
    token: str = "",
    responsabilidad: str | None = Query(default=None),
    cod_bodega: str | None = Query(default=None),
    dias_periodo: int = Query(default=1, ge=1, le=366),
):
    _check_token(token)
    try:
        rows_mp = leer_bd_mp_sistema()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"No se pudo leer BD_MP_SISTEMA: {e}") from e
    return build_inventario_vivo(
        rows_mp,
        responsabilidad=responsabilidad,
        cod_bodega=cod_bodega,
        dias_periodo=dias_periodo,
    )


@router.get("/api/dashboard/rentabilidad")
def dashboard_rentabilidad(
    token: str = "",
    desde: str | None = Query(default=None),
    hasta: str | None = Query(default=None),
    agrup: str = Query(default="mes"),
):
    _check_token(token)
    sb = _get_sb()
    catalogo = _cargar_catalogo()
    desde = _sanitize_fecha(desde)
    hasta = _sanitize_fecha(hasta)
    if not desde or not hasta:
        raise HTTPException(status_code=400, detail="desde y hasta son obligatorios")
    ventas = _query_hist_ventas(sb, desde=desde, hasta=hasta)
    entradas = [
        m for m in _query_mov_inventario(sb, desde=desde, hasta=hasta)
        if (m.get("tipo_mov") or "") == "ENTRADA"
    ]
    return build_rentabilidad_from_catalog(
        rows_ventas=ventas,
        rows_entrada=entradas,
        catalogo=catalogo,
        resolver=_resolver_producto,
        neto_fn=_neto_linea,
        desde=date.fromisoformat(desde),
        hasta=date.fromisoformat(hasta),
        agrup=agrup,
    )


@router.get("/api/dashboard/roturas")
def dashboard_roturas(
    token: str = "",
    desde: str | None = Query(default=None),
    hasta: str | None = Query(default=None),
    agrup: str = Query(default="mes"),
    bodega: str | None = Query(default=None),
):
    _check_token(token)
    sb = _get_sb()
    desde = _sanitize_fecha(desde)
    hasta = _sanitize_fecha(hasta)
    if not desde or not hasta:
        raise HTTPException(status_code=400, detail="desde y hasta son obligatorios")
    movs = _query_mov_inventario(sb, desde=desde, hasta=hasta)
    return build_roturas_historico(
        movs, desde=date.fromisoformat(desde), hasta=date.fromisoformat(hasta), agrup=agrup, bodega=bodega
    )


@router.get("/api/dashboard/confianza")
def dashboard_confianza(token: str = ""):
    _check_token(token)
    sb = _get_sb()
    ciclos = _query_conteo_ciclos(sb)
    try:
        inv = build_inventario_vivo(leer_bd_mp_sistema())
        neg = inv["resumen"].get("NEGATIVO", 0)
    except Exception:
        neg = 0
    return build_confianza_inventario(ciclos=ciclos, detalles=[], stock_negativos=neg)

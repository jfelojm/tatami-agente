"""
Interpreta cantidades en lenguaje natural para traslados MP y producción de subrecetas.

Ejemplos:
  - «una botella de Buchanan's Master» → 750 ml (factor_conversion en BD_ITEMS_PROV)
  - «6 tortas de chocolate» → 6 × rendimiento_estandar de la subreceta
"""

from __future__ import annotations

import re
import time
import unicodedata

from numeros_sheets import parse_numero_sheets

_NUM_PALABRAS = {
    "un": 1,
    "una": 1,
    "uno": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
}

_UNIDADES_PRESENTACION_MP = (
    "botella",
    "botellas",
    "caja",
    "cajas",
    "pack",
    "packs",
    "paquete",
    "paquetes",
    "lata",
    "latas",
    "bolsa",
    "bolsas",
    "bulto",
    "bultos",
    "garrafa",
    "garrafas",
    "frasco",
    "frascos",
    "unidad",
    "unidades",
    "uni",
)

_UNIDADES_LOTE_SUB = (
    "torta",
    "tortas",
    "lote",
    "lotes",
    "batch",
    "batches",
    "porcion",
    "porciones",
    "porción",
    "porciones",
    "unidad",
    "unidades",
    "preparacion",
    "preparación",
    "preparaciones",
    "receta",
    "recetas",
)

_PRESENTACION_RE = re.compile(
    r"(?P<cant>"
    r"\d[\d.,]*|"
    + "|".join(re.escape(w) for w in sorted(_NUM_PALABRAS, key=len, reverse=True))
    + r")\s+"
    r"(?P<unidad>"
    + "|".join(_UNIDADES_PRESENTACION_MP + _UNIDADES_LOTE_SUB)
    + r")\b",
    re.I,
)

_EXPLICITO_BASE_RE = re.compile(
    r"(\d[\d.,]*)\s*(?:ml|mililitros?|gr|gramos?|g\b|kg|kilos?|litros?|l\b|lt\b)",
    re.I,
)

_ITEMS_PROV_CACHE: dict[str, dict] | None = None
_ITEMS_PROV_CACHE_AT: float = 0.0
_ITEMS_PROV_TTL_SEC = 300.0

_REND_SUB_CACHE: dict[str, dict] | None = None
_REND_SUB_CACHE_AT: float = 0.0
_REND_SUB_TTL_SEC = 300.0


def _norm_txt(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _parse_cantidad_token(tok: str) -> float | None:
    t = (tok or "").strip().lower()
    if not t:
        return None
    if t in _NUM_PALABRAS:
        return float(_NUM_PALABRAS[t])
    try:
        return float(t.replace(",", "."))
    except ValueError:
        return None


def parse_cantidad_explicita_base(texto: str) -> float | None:
    """Cantidad ya en unidad base: 750 ml, 1054 gr, etc."""
    m = _EXPLICITO_BASE_RE.search(texto or "")
    if not m:
        return None
    val = _parse_cantidad_token(m.group(1))
    if val is None:
        return None
    frag = _norm_txt(m.group(0))
    if "kg" in frag or "kilo" in frag:
        return val * 1000.0
    if "litro" in frag or re.search(r"\blt?\b", frag):
        return val * 1000.0
    return val


def _singular_presentacion(un: str) -> str:
    u = (un or "").strip().lower()
    mapping = {
        "unidades": "unidad",
        "uni": "unidad",
        "unidad": "unidad",
        "tortas": "torta",
        "torta": "torta",
        "lotes": "lote",
        "lote": "lote",
        "batches": "batch",
        "batch": "batch",
        "porciones": "porcion",
        "porción": "porcion",
        "porcion": "porcion",
        "preparaciones": "preparacion",
        "preparación": "preparacion",
        "preparacion": "preparacion",
        "recetas": "receta",
        "receta": "receta",
        "botellas": "botella",
        "botella": "botella",
        "cajas": "caja",
        "caja": "caja",
        "packs": "pack",
        "pack": "pack",
        "paquetes": "paquete",
        "paquete": "paquete",
        "latas": "lata",
        "lata": "lata",
        "bolsas": "bolsa",
        "bolsa": "bolsa",
        "bultos": "bulto",
        "bulto": "bulto",
        "garrafas": "garrafa",
        "garrafa": "garrafa",
        "frascos": "frasco",
        "frasco": "frasco",
    }
    return mapping.get(u, u)


def parse_cantidad_presentacion(texto: str) -> tuple[float, str] | None:
    """
    «una botella», «6 tortas» → (cantidad, unidad_normalizada).
    unidad_normalizada: botella | torta | lote | ...
    """
    t = _norm_txt(texto)
    m = _PRESENTACION_RE.search(t)
    if not m:
        return None
    cant = _parse_cantidad_token(m.group("cant"))
    if cant is None or cant <= 0:
        return None
    un = _singular_presentacion(m.group("unidad"))
    if un == "botella":
        pass
    elif un in ("caja", "pack", "paquete", "lata", "bolsa", "bulto", "garrafa", "frasco"):
        pass
    elif un in ("torta", "porcion", "preparacion", "receta"):
        un = "lote"
    elif un == "batch":
        un = "lote"
    elif un in ("unidad", "uni"):
        un = "unidad"
    return cant, un


def _norm_mp(cod: str) -> str:
    c = (cod or "").strip().upper()
    if c.startswith("MP-"):
        return c
    if c.isdigit():
        return f"MP-{c}"
    return c


def _norm_sub_cod(cod: str) -> str:
    from codigos_subreceta import cod_sub_canonico

    return cod_sub_canonico(cod)


def cargar_factores_mp_por_cod(*, sh=None, force_refresh: bool = False) -> dict[str, dict]:
    """cod_mp_norm → factor_conversion, unidad_compra, unidad_base."""
    global _ITEMS_PROV_CACHE, _ITEMS_PROV_CACHE_AT
    now = time.monotonic()
    if (
        not force_refresh
        and _ITEMS_PROV_CACHE is not None
        and (now - _ITEMS_PROV_CACHE_AT) < _ITEMS_PROV_TTL_SEC
    ):
        return _ITEMS_PROV_CACHE

    if sh is None:
        import os

        import gspread
        from dotenv import load_dotenv
        from google_credentials import google_credentials

        load_dotenv(override=False)
        from google_credentials import google_credentials, open_gspread_workbook, pin_cloud_env

        pin_cloud_env()
        sh = open_gspread_workbook(["https://www.googleapis.com/auth/spreadsheets"])

    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = next((i for i, r in enumerate(vals) if "cod_mp_sistema" in r), None)
    if hi is None:
        _ITEMS_PROV_CACHE = {}
        _ITEMS_PROV_CACHE_AT = now
        return {}

    h = [(c or "").strip() for c in vals[hi]]
    idx = {x: h.index(x) for x in h if x}

    def cell(row, col):
        j = idx.get(col)
        if j is None or j >= len(row):
            return ""
        return row[j]

    out: dict[str, dict] = {}
    for row in vals[hi + 1 :]:
        if str(row[0]).startswith("[FK]"):
            continue
        mp = _norm_mp(cell(row, "cod_mp_sistema"))
        if not mp:
            continue
        activo = (cell(row, "activo") or "SI").strip().upper()
        if activo == "NO":
            continue
        fac = parse_numero_sheets(cell(row, "factor_conversion"), 1.0)
        if fac <= 0:
            fac = 1.0
        uc = _norm_txt(cell(row, "unidad_compra") or "uni")
        ub = (cell(row, "unidad_base_sistema") or "gr").strip().lower() or "gr"
        prev = out.get(mp)
        if prev and prev.get("factor_conversion", 1) > 1 and fac <= 1:
            continue
        out[mp] = {
            "factor_conversion": fac,
            "unidad_compra": uc,
            "unidad_base": ub,
        }

    _ITEMS_PROV_CACHE = out
    _ITEMS_PROV_CACHE_AT = now
    return out


def cargar_rendimiento_subrecetas(*, sh=None, force_refresh: bool = False) -> dict[str, dict]:
    """cod_sub canonico → rendimiento_estandar, unidad, nombre."""
    global _REND_SUB_CACHE, _REND_SUB_CACHE_AT
    now = time.monotonic()
    if (
        not force_refresh
        and _REND_SUB_CACHE is not None
        and (now - _REND_SUB_CACHE_AT) < _REND_SUB_TTL_SEC
    ):
        return _REND_SUB_CACHE

    if sh is None:
        import os

        import gspread
        from dotenv import load_dotenv
        from google_credentials import google_credentials

        load_dotenv(override=False)
        from google_credentials import google_credentials, open_gspread_workbook, pin_cloud_env

        pin_cloud_env()
        sh = open_gspread_workbook(["https://www.googleapis.com/auth/spreadsheets"])

    from subrecetas_detalle import cargar_bd_subrecetas

    cab = cargar_bd_subrecetas(sh)

    out: dict[str, dict] = {}
    for cod_raw, info in cab.items():
        if (info.get("activa") or "SI").strip().upper() == "NO":
            continue
        cod = _norm_sub_cod(cod_raw)
        rend = parse_numero_sheets(info.get("rendimiento_estandar"), 0.0)
        if rend <= 0:
            continue
        out[cod] = {
            "rendimiento_estandar": rend,
            "unidad": (info.get("unidad") or info.get("unidad_base") or "gr").strip().lower(),
            "nombre_subreceta": (info.get("nombre_subreceta") or "").strip(),
        }

    _REND_SUB_CACHE = out
    _REND_SUB_CACHE_AT = now
    return out


def _unidad_compra_coincide(unidad_pedida: str, unidad_catalogo: str) -> bool:
    up = _norm_txt(unidad_pedida)
    uc = _norm_txt(unidad_catalogo)
    if not up or not uc:
        return False
    if up == uc or up.rstrip("s") == uc.rstrip("s"):
        return True
    aliases = {
        "botella": ("botella", "bot", "btl"),
        "caja": ("caja", "box"),
        "pack": ("pack", "paquete", "pqte"),
        "lata": ("lata", "lat"),
        "unidad": ("unidad", "uni", "und"),
    }
    for key, vals in aliases.items():
        if up in vals and any(v in uc for v in vals):
            return True
    return up in uc or uc in up


def resolver_cantidad_traslado_mp(
    cod_mp: str,
    cantidad: float,
    *,
    unidad_base: str = "",
    texto: str = "",
    cantidad_presentacion: float | None = None,
    unidad_presentacion: str = "",
    catalogo_mp: dict[str, dict] | None = None,
) -> dict:
    """
    Convierte cantidad pedida a unidad_base del inventario (gr/ml/uni).

    Retorna: cantidad_base, interpretacion, factor_usado, unidad_compra.
    """
    cod = _norm_mp(cod_mp)
    cat = catalogo_mp if catalogo_mp is not None else cargar_factores_mp_por_cod()
    info = cat.get(cod, {})
    factor = float(info.get("factor_conversion") or 1.0)
    uc = (info.get("unidad_compra") or "uni").strip()
    ub = (unidad_base or info.get("unidad_base") or "gr").strip().lower()

    expl = parse_cantidad_explicita_base(texto)
    if expl is not None and expl > 0:
        return {
            "cantidad_base": expl,
            "interpretacion": f"{expl:g} {ub} (cantidad explícita en texto)",
            "factor_usado": None,
            "unidad_compra": uc,
        }

    pres = None
    if cantidad_presentacion is not None and cantidad_presentacion > 0:
        pres = (cantidad_presentacion, _norm_txt(unidad_presentacion or uc or "unidad"))
    elif texto:
        pres = parse_cantidad_presentacion(texto)

    if pres:
        n_pres, un_pres = pres
        if un_pres in ("lote", "torta", "batch"):
            un_pres = uc or "unidad"
        if factor > 1 and _unidad_compra_coincide(un_pres, uc):
            base = n_pres * factor
            return {
                "cantidad_base": base,
                "interpretacion": (
                    f"{n_pres:g} {un_pres} × {factor:g} {ub}/{uc or 'unidad compra'}"
                ),
                "factor_usado": factor,
                "unidad_compra": uc,
            }

    cant = float(cantidad or 0)
    if cant <= 0:
        return {
            "cantidad_base": cant,
            "interpretacion": "cantidad inválida",
            "factor_usado": None,
            "unidad_compra": uc,
        }

    # Heurística: cantidad pequeña entera + factor grande → probablemente unidades de compra
    if (
        factor > 1
        and cant == int(cant)
        and 0 < cant <= 50
        and cant < factor
        and (not texto or parse_cantidad_presentacion(texto) or cant <= 20)
    ):
        base = cant * factor
        return {
            "cantidad_base": base,
            "interpretacion": (
                f"{cant:g} {uc or 'unidad compra'} × {factor:g} {ub} "
                f"(interpretado desde catálogo)"
            ),
            "factor_usado": factor,
            "unidad_compra": uc,
        }

    return {
        "cantidad_base": cant,
        "interpretacion": f"{cant:g} {ub} (sin conversión)",
        "factor_usado": None,
        "unidad_compra": uc,
    }


def resolver_cantidad_produccion_sub(
    cod_sub: str,
    cantidad: float | None,
    *,
    texto: str = "",
    cantidad_lotes: float | None = None,
    catalogo_sub: dict[str, dict] | None = None,
) -> dict:
    """
    Convierte «6 tortas» o cantidad pequeña a gr/ml del lote.

    cantidad=None y sin lotes → None (usa rendimiento estándar en planificar).
    """
    cod = _norm_sub_cod(cod_sub)
    cat = catalogo_sub if catalogo_sub is not None else cargar_rendimiento_subrecetas()
    info = cat.get(cod, {})
    rend = float(info.get("rendimiento_estandar") or 0)
    un = (info.get("unidad") or "gr").strip().lower()
    nom = info.get("nombre_subreceta") or cod

    expl = parse_cantidad_explicita_base(texto)
    if expl is not None and expl > 0:
        return {
            "cantidad_base": expl,
            "interpretacion": f"{expl:g} {un} (cantidad explícita)",
            "rendimiento_estandar": rend,
            "lotes": expl / rend if rend > 0 else None,
        }

    lotes: float | None = cantidad_lotes
    pres = parse_cantidad_presentacion(texto) if texto else None
    if pres:
        n_pres, un_pres = pres
        if un in ("uni", "unidad") and un_pres in ("unidad", "uni"):
            return {
                "cantidad_base": n_pres,
                "interpretacion": f"{n_pres:g} uni ({nom})",
                "rendimiento_estandar": rend,
                "lotes": n_pres / rend if rend > 0 else None,
            }
        elif un_pres in ("lote", "torta", "batch") or (
            un_pres in _UNIDADES_LOTE_SUB and un_pres not in ("unidad", "uni")
        ):
            lotes = n_pres

    if lotes is not None and lotes > 0 and rend > 0:
        base = lotes * rend
        return {
            "cantidad_base": base,
            "interpretacion": f"{lotes:g} lote(s) × {rend:g} {un} ({nom})",
            "rendimiento_estandar": rend,
            "lotes": lotes,
        }

    if cantidad is not None and cantidad > 0:
        cant = float(cantidad)
        menciona_uni = bool(
            re.search(r"\b(uni(?:dad(?:es)?)?)\b", _norm_txt(texto or ""))
        )
        if un in ("uni", "unidad") and (menciona_uni or (rend > 0 and cant > rend)):
            return {
                "cantidad_base": cant,
                "interpretacion": f"{cant:g} uni ({nom})",
                "rendimiento_estandar": rend,
                "lotes": cant / rend if rend > 0 else None,
            }
        # Entero pequeño < rendimiento → lotes solo en gr/ml (ej. «6» torta chocolate = 6×1054 gr).
        # En uni el número es piezas (ej. «10 tarta vasca» = 10 uni, no 10×16).
        if (
            rend > 0
            and cant == int(cant)
            and 0 < cant <= 30
            and cant < rend
            and un not in ("uni", "unidad")
        ):
            base = cant * rend
            return {
                "cantidad_base": base,
                "interpretacion": f"{cant:g} lote(s) × {rend:g} {un} ({nom})",
                "rendimiento_estandar": rend,
                "lotes": cant,
            }
        return {
            "cantidad_base": cant,
            "interpretacion": f"{cant:g} {un} (sin conversión a lotes)",
            "rendimiento_estandar": rend,
            "lotes": cant / rend if rend > 0 else None,
        }

    return {
        "cantidad_base": None,
        "interpretacion": f"lote estándar {rend:g} {un}" if rend > 0 else "lote estándar",
        "rendimiento_estandar": rend,
        "lotes": 1.0 if rend > 0 else None,
    }

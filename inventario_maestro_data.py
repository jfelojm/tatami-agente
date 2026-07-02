"""
Lectura unificada de BD_MP_SISTEMA: Google Sheets con fallback a Supabase.

Cuando Railway no puede leer Sheets (PermissionError en credenciales GCP),
los traslados masivos y otros flujos operativos siguen usando mov_inventario.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Any

from bodegas_config import BODEGAS, normalizar_cod_bodega
from inventario_stock_mp import norm_mp

_CACHE: list[dict] | None = None
_CACHE_AT: float = 0.0
_CACHE_TTL_SEC = 60.0
_CACHE_FUENTE: str = ""


def conectar_supabase():
    from supabase import create_client

    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def filas_bd_mp_desde_supabase() -> list[dict]:
    """Filas estilo BD_MP_SISTEMA desde mov_inventario (stock + metadatos recientes)."""
    from recalcular_stock_sheets import TIPOS_SUMA_DESTINO, _clave_stock, build_stock_calculado, paginar_todo

    stock_map = build_stock_calculado()
    movs = paginar_todo(
        "mov_inventario",
        "cod_mp_sistema,nombre_mp,unidad_base,tipo_mov,cod_bodega_origen,"
        "cod_bodega_destino,costo_unitario,fecha",
    )

    meta: dict[tuple[str, str], dict[str, str]] = {}
    costo_ref: dict[tuple[str, str], float] = {}

    for m in movs:
        cod_raw = (m.get("cod_mp_sistema") or "").strip()
        if not cod_raw:
            continue
        cod = norm_mp(cod_raw) if cod_raw.isdigit() else cod_raw.upper()
        nombre = (m.get("nombre_mp") or "").strip()
        unidad = (m.get("unidad_base") or "").strip()
        tipo = (m.get("tipo_mov") or "").strip()

        bods: list[str] = []
        if m.get("cod_bodega_origen"):
            bods.append(normalizar_cod_bodega(m["cod_bodega_origen"]))
        if m.get("cod_bodega_destino"):
            bods.append(normalizar_cod_bodega(m["cod_bodega_destino"]))

        for bod in bods:
            if not bod or bod not in BODEGAS or not BODEGAS[bod].activa:
                continue
            k = _clave_stock(cod, bod)
            slot = meta.setdefault(
                k,
                {"cod_mp_sistema": cod, "cod_bodega": bod, "nombre_mp": nombre or cod, "unidad_base": unidad},
            )
            if nombre:
                slot["nombre_mp"] = nombre
            if unidad and not slot["unidad_base"]:
                slot["unidad_base"] = unidad
            if tipo in TIPOS_SUMA_DESTINO:
                try:
                    cu = float(m.get("costo_unitario") or 0)
                except (TypeError, ValueError):
                    cu = 0.0
                if cu > 0:
                    costo_ref[k] = cu

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for k, stock in stock_map.items():
        cod, bod = k
        seen.add(k)
        m = meta.get(k, {})
        rows.append(
            {
                "cod_mp_sistema": cod,
                "cod_bodega": bod,
                "nombre_mp": m.get("nombre_mp") or cod,
                "unidad_base": m.get("unidad_base")
                or ("gr" if str(cod).upper().startswith("SUB-") else "uni"),
                "stock_actual": round(float(stock), 4),
                "costo_unitario_ref": costo_ref.get(k, 0),
            }
        )

    for k, m in meta.items():
        if k in seen:
            continue
        cod, bod = k
        rows.append(
            {
                "cod_mp_sistema": cod,
                "cod_bodega": bod,
                "nombre_mp": m.get("nombre_mp") or cod,
                "unidad_base": m.get("unidad_base")
                or ("gr" if str(cod).upper().startswith("SUB-") else "uni"),
                "stock_actual": 0.0,
                "costo_unitario_ref": costo_ref.get(k, 0),
            }
        )
    return rows


def leer_bd_mp_sistema_unificado(*, force_refresh: bool = False) -> tuple[list[dict], str]:
    """
    Retorna (filas BD_MP_SISTEMA, fuente).
    fuente: 'sheets' | 'supabase'
    """
    global _CACHE, _CACHE_AT, _CACHE_FUENTE
    now = time.monotonic()
    if (
        not force_refresh
        and _CACHE is not None
        and (now - _CACHE_AT) < _CACHE_TTL_SEC
    ):
        return list(_CACHE), _CACHE_FUENTE

    fuente = "supabase"
    rows: list[dict] = []
    try:
        from dashboard_services.sheets_data import leer_bd_mp_sistema

        sheet_rows = leer_bd_mp_sistema()
        if sheet_rows:
            rows = sheet_rows
            fuente = "sheets"
    except Exception as e:
        print(f"WARN inventario_maestro: Sheets no disponible ({type(e).__name__}: {e})")

    if not rows:
        rows = filas_bd_mp_desde_supabase()
        fuente = "supabase"

    _CACHE = rows
    _CACHE_AT = now
    _CACHE_FUENTE = fuente
    return list(rows), fuente


def extraer_cod_producto(texto: str) -> str:
    """Código MP o SUB desde línea del catálogo Sheets."""
    s = unicodedata.normalize("NFKC", str(texto or "").strip())
    m = re.search(r"\b(SUB-\d{2,4})\b", s, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\bSUB\s*[-_]?\s*(\d{2,4})\b", s, re.I)
    if m:
        return f"SUB-{m.group(1).zfill(3)}"
    parts = re.split(r"[\t|—–\-]+", s)
    for p in parts:
        p = p.strip()
        if re.fullmatch(r"SUB-\d{2,4}", p, re.I):
            return p.upper()
        if re.fullmatch(r"\d{2,4}", p):
            return p
    m = re.search(r"\b(\d{2,4})\b", s)
    return m.group(1) if m else s.strip()


def _nombre_desde_linea_catalogo(producto: str) -> str:
    parts = re.split(r"[\t|—–\-]+", str(producto or "").strip())
    for p in parts:
        p = p.strip()
        if p and not re.fullmatch(r"SUB-\d{2,4}", p, re.I) and not re.fullmatch(r"\d+(?:\.\d+)?", p):
            if p.lower() not in ("gr", "ml", "uni", "kg", "lt", "l"):
                return p
    return ""


def _unidad_desde_linea_catalogo(producto: str) -> str:
    parts = re.split(r"[\t|—–\-]+", str(producto or "").strip())
    for p in reversed(parts):
        u = p.strip().lower()
        if u in ("gr", "ml", "uni", "kg", "lt", "l", "unidad"):
            return "uni" if u in ("uni", "unidad") else u
    return ""


def _filas_mp(rows: list[dict], cod: str) -> list[dict]:
    c = norm_mp(cod)
    return [r for r in rows if norm_mp(r.get("cod_mp_sistema")) == c]


def resolver_mp_linea_traslado(
    rows: list[dict],
    *,
    producto: str = "",
    cod_mp: str = "",
    bodega_origen: str = "",
) -> dict[str, Any]:
    """
    Resuelve MP/SUB para traslado masivo sin depender de whatsapp_webhook ni Sheets extra.
    Las líneas del catálogo INGRESO_TRASLADO ya traen SUB-xxx o código numérico.
    """
    prod = (producto or "").strip()
    cod_raw = (cod_mp or "").strip() or extraer_cod_producto(prod)
    if not cod_raw:
        return {"ok": False, "error": "Sin código de producto en la línea"}

    cod_upper = cod_raw.upper()
    if cod_upper.startswith("SUB-"):
        from codigos_subreceta import cod_sub_canonico

        cod_ok = cod_sub_canonico(cod_upper)
        nombre = _nombre_desde_linea_catalogo(prod) or cod_ok
        unidad = _unidad_desde_linea_catalogo(prod)
        for r in _filas_mp(rows, cod_ok):
            if (r.get("nombre_mp") or "").strip():
                nombre = r["nombre_mp"]
            if not unidad:
                unidad = str(r.get("unidad_base") or "").strip()
            break
        return {
            "ok": True,
            "cod_mp": cod_ok,
            "nombre_mp": nombre,
            "unidad_base": unidad or "gr",
        }

    cod_norm = norm_mp(cod_raw)
    filas = _filas_mp(rows, cod_norm)
    if filas:
        nombre = next((str(r.get("nombre_mp") or "").strip() for r in filas if r.get("nombre_mp")), cod_norm)
        return {"ok": True, "cod_mp": cod_norm, "nombre_mp": nombre or cod_norm}

    # Búsqueda por nombre en catálogo (substring)
    tokens = [t for t in re.split(r"\s+", _nombre_desde_linea_catalogo(prod).lower()) if len(t) >= 2]
    if not tokens:
        tokens = [t for t in re.split(r"\s+", prod.lower()) if len(t) >= 3]
    if tokens:
        origen = normalizar_cod_bodega(bodega_origen)
        hits: dict[str, dict] = {}
        for r in rows:
            nom = str(r.get("nombre_mp") or "").lower()
            cod = norm_mp(r.get("cod_mp_sistema"))
            if not cod or not nom:
                continue
            if all(tok in nom for tok in tokens):
                bod = normalizar_cod_bodega(r.get("cod_bodega"))
                prio = 0 if bod == origen else 1
                prev = hits.get(cod)
                if prev is None or prio < prev["_prio"]:
                    hits[cod] = {**r, "_prio": prio}
        if len(hits) == 1:
            h = next(iter(hits.values()))
            return {
                "ok": True,
                "cod_mp": norm_mp(h.get("cod_mp_sistema")),
                "nombre_mp": (h.get("nombre_mp") or cod_norm).strip(),
            }
        if len(hits) > 1:
            opciones = sorted({norm_mp(h.get("cod_mp_sistema")) for h in hits.values()})[:8]
            return {
                "ok": False,
                "error": f"Varios productos coinciden con '{prod}': {', '.join(opciones)}",
            }

    return {"ok": False, "error": f"No encontré '{prod or cod_raw}' en inventario (maestro/Supabase)"}

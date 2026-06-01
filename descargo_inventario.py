import argparse
import difflib
import os
import re
import uuid
from datetime import date, timedelta
from datetime import datetime

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client

from matching_productos import (
    cargar_bd_productos,
    construir_lookup,
    resolver_match,
)
from ventas_smartmenu import estado_documento_excluye_neto_operativo
from bodegas_config import (
    normalizar_cod_bodega,
    resolver_bodega_receta,
)
from descargo_subreceta import (
    PREFIJO_PSEUDO_MP,
    cargar_metadata_subrecetas,
    descargo_subrecetas_habilitado,
    preparar_ingredientes_descargo,
    procesar_linea_sub_venta,
)
from recetas_detalle import es_linea_mp

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _sheet_float(v) -> float:
    """Números desde Sheets (coma/punto/miles según formato de celda)."""
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, 0.0)


def _iso_fecha_hora_mov(fecha_v: str | None, hora_raw: str | None) -> str:
    """ISO 8601 local para Postgres: evita duplicar ':00' si hora ya trae segundos."""
    fecha = (fecha_v or "").strip() or "1970-01-01"
    h = (hora_raw or "").strip()
    if not h:
        return f"{fecha}T00:00:00"
    parts = h.split(":")
    try:
        if len(parts) == 2:
            return f"{fecha}T{int(parts[0]):02d}:{int(parts[1]):02d}:00"
        if len(parts) >= 3:
            sec = parts[2].split(".")[0]
            return f"{fecha}T{int(parts[0]):02d}:{int(parts[1]):02d}:{int(sec or 0):02d}"
    except ValueError:
        pass
    return f"{fecha}T00:00:00"


def _limpiar_variedad(variedad: str | None) -> str:
    s = (variedad or "").strip().upper()
    for ch in ("\u00a0", "\u2007", "\u2009", "\u202f", "\ufeff"):
        s = s.replace(ch, " ")
    if "OBS:" in s:
        s = s.split("OBS:", 1)[0].strip()
    return " ".join(s.split())


def _mismo_cod_receta(a: str, b: str) -> bool:
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    if a.isdigit() and b.isdigit():
        return int(a) == int(b)
    return False


def _var_compact(variedad: str | None) -> str:
    """Variedad sin espacios (para tolerar '330 ML' vs '330ML')."""
    return "".join(_limpiar_variedad(variedad).split())


def _var_alnum_key(variedad: str | None) -> str:
    """Letras y dígitos solamente (ignora espacios, paréntesis, puntos en '330 ML.', etc.)."""
    s = _limpiar_variedad(variedad)
    return re.sub(r"[^A-ZÁÉÍÓÚÜÑ0-9]", "", s)


def _ingredientes_mejor_variedad_por_similitud(
    candidatas: list[dict], var_pos: str, cod_receta_log: str
) -> list[dict] | None:
    """
    Varias variedades no vacías en BD_RECETAS_DETALLE para la misma receta:
    elige la que mejor se parece al texto del POS (difflib + refuerzo si clave alfanumérica
    es subcadena de la otra). Desempate por coincidencia de dígitos (330 vs 473).
    """
    if not var_pos or not candidatas:
        return None
    dist = sorted(
        {
            _limpiar_variedad(r.get("variedad_smart_menu", ""))
            for r in candidatas
            if _limpiar_variedad(r.get("variedad_smart_menu", ""))
        }
    )
    if len(dist) < 2:
        return None

    def filas_exactas_local(pool: list[dict], v: str) -> list[dict]:
        out = []
        for r in pool:
            r_var = _limpiar_variedad(r.get("variedad_smart_menu", ""))
            if v and r_var != v:
                continue
            if not v and r_var:
                continue
            out.append(r)
        return out

    def _digitos(s: str) -> str:
        return "".join(re.findall(r"\d+", _limpiar_variedad(s)))

    dig_pos = _digitos(var_pos)

    scored: list[tuple[float, str]] = []
    for rv in dist:
        ratio = difflib.SequenceMatcher(None, var_pos, rv).ratio()
        ak_v, ak_r = _var_alnum_key(var_pos), _var_alnum_key(rv)
        if ak_v and ak_r:
            if ak_v == ak_r:
                ratio = 1.0
            elif ak_v in ak_r or ak_r in ak_v:
                ratio = max(ratio, 0.88)
        # Misma capacidad numérica (330 vs 330ML)
        if dig_pos and _digitos(rv) and (
            dig_pos == _digitos(rv)
            or dig_pos in _digitos(rv)
            or _digitos(rv) in dig_pos
        ):
            ratio = max(ratio, 0.86)
        scored.append((ratio, rv))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_ratio, best_rv = scored[0][0], scored[0][1]
    if best_ratio < 0.40:
        return None
    # Desempate: entre candidatos con ratio similar, preferir mismo "número de envase"
    close = [s for s in scored if s[0] >= best_ratio - 0.07]
    if len(close) > 1 and dig_pos:
        with_dig = [s for s in close if dig_pos and _digitos(s[1]) and (dig_pos in _digitos(s[1]) or _digitos(s[1]) in dig_pos or dig_pos == _digitos(s[1]))]
        if with_dig:
            with_dig.sort(key=lambda x: (-x[0], x[1]))
            best_ratio, best_rv = with_dig[0][0], with_dig[0][1]
        else:
            best_ratio, best_rv = close[0][0], close[0][1]
    elif len(scored) > 1 and scored[1][0] >= best_ratio - 0.04 and not dig_pos:
        return None

    print(
        f"    INFO: receta={cod_receta_log} — variedad por similitud ({best_ratio:.0%}): "
        f"hoja={best_rv!r} POS={var_pos!r}"
    )
    return filas_exactas_local(candidatas, best_rv)


# ── GOOGLE SHEETS ─────────────────────────────────────────────
def _get_sheet():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


# ── CARGA BD_RECETAS_DETALLE ──────────────────────────────────
_recetas_cache = None


def cargar_recetas() -> list[dict]:
    global _recetas_cache
    if _recetas_cache is not None:
        return _recetas_cache

    print("  Cargando BD_RECETAS_DETALLE...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    values = ws.get_all_values()
    header_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_receta" for c in row):
            header_idx = i
            break
    if header_idx is None:
        print("  ERROR: BD_RECETAS_DETALLE sin fila de cabecera con columna cod_receta")
        _recetas_cache = []
        return []

    headers = [(c or "").strip() for c in values[header_idx]]
    if "cod_receta" not in headers:
        print("  ERROR: BD_RECETAS_DETALLE sin columna cod_receta")
        _recetas_cache = []
        return []

    result = []
    for j in range(header_idx + 1, len(values)):
        row = values[j]
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        r = {
            headers[k]: (row[k] if k < len(row) else "").strip()
            for k in range(min(len(headers), len(row)))
            if headers[k]
        }
        if not (r.get("cod_receta") or "").strip():
            continue
        if not es_linea_mp(r) and not (r.get("cod_subreceta") or "").strip():
            continue
        result.append(r)

    print(f"  {len(result)} ingredientes cargados")
    _recetas_cache = result
    return result


def get_ingredientes(cod_receta: str, variedad: str | None) -> list[dict]:
    recetas = cargar_recetas()
    cod = cod_receta.strip()
    var = _limpiar_variedad(variedad)

    candidatas = [
        r for r in recetas if _mismo_cod_receta(r.get("cod_receta", ""), cod)
    ]
    if not candidatas:
        print(
            f"    INFO: cod_receta={cod!r} sin filas en BD_RECETAS_DETALLE "
            "(revise cod_receta o filas sin cod_mp_sistema / comentadas con #)."
        )
        return []

    def filas_exactas(pool: list[dict], v: str) -> list[dict]:
        out = []
        for r in pool:
            r_var = _limpiar_variedad(r.get("variedad_smart_menu", ""))
            if v and r_var != v:
                continue
            if not v and r_var:
                continue
            out.append(r)
        return out

    resultado = filas_exactas(candidatas, var)
    if resultado:
        return resultado
    if var:
        sin_var = filas_exactas(candidatas, "")
        if sin_var:
            return sin_var
        fuzzy = []
        for r in candidatas:
            r_var = _limpiar_variedad(r.get("variedad_smart_menu", ""))
            if not r_var:
                continue
            if r_var in var or var in r_var:
                fuzzy.append(r)
        if fuzzy:
            return fuzzy
        vc = _var_compact(var)
        if vc:
            fuzzy_c = [
                r
                for r in candidatas
                if _var_compact(r.get("variedad_smart_menu", "")) == vc
            ]
            if fuzzy_c:
                return fuzzy_c
        va = _var_alnum_key(var)
        if va:
            fuzzy_a = [
                r
                for r in candidatas
                if _var_alnum_key(r.get("variedad_smart_menu", "")) == va
            ]
            if fuzzy_a:
                return fuzzy_a
        # Una sola variedad no vacía en la hoja: usarla aunque el POS acorte el texto.
        # Si además hay filas con variedad vacía (receta genérica), igual aplica la única SKU
        # concreta (caso típico bebidas en carta).
        non_empty_vars = sorted(
            {
                _limpiar_variedad(r.get("variedad_smart_menu", ""))
                for r in candidatas
                if _limpiar_variedad(r.get("variedad_smart_menu", ""))
            }
        )
        if len(non_empty_vars) == 1:
            unica = non_empty_vars[0]
            print(
                f"    INFO: receta={cod} — usando única variedad en hoja "
                f"'{unica}' (POS '{var}' no coincidía tras normalizar)."
            )
            return filas_exactas(candidatas, unica)
        sim = _ingredientes_mejor_variedad_por_similitud(candidatas, var, cod)
        if sim:
            return sim
    if candidatas and var:
        distintos = sorted(
            {
                _limpiar_variedad(r.get("variedad_smart_menu", ""))
                for r in candidatas
                if _limpiar_variedad(r.get("variedad_smart_menu", ""))
            }
        )
        print(
            f"    INFO: receta={cod} tiene {len(candidatas)} filas en BD_RECETAS_DETALLE "
            f"pero ninguna coincide con variedad buscada '{var}'. "
            f"Variedades en hoja (no vacías): {distintos[:20]}"
            + (" …" if len(distintos) > 20 else "")
        )
    return []


# ── CARGA BD_MP_SISTEMA ───────────────────────────────────────
_mp_cache: dict[tuple[str, str], dict] | None = None
_mp_row_index: dict[tuple[str, str], int] = {}
_mp_stock_col_idx: int | None = None


def _mp_key(cod_mp: str, cod_bodega: str) -> tuple[str, str]:
    return (cod_mp.strip(), normalizar_cod_bodega(cod_bodega))


def cargar_mp_sistema() -> dict[tuple[str, str], dict]:
    """Clave (cod_mp_sistema, cod_bodega) → fila maestro."""
    global _mp_cache, _mp_row_index, _mp_stock_col_idx

    if _mp_cache is not None:
        return _mp_cache

    print("  Cargando BD_MP_SISTEMA...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if any(c.strip() == "cod_mp_sistema" for c in row):
            header_row_idx = i
            break

    if header_row_idx is None:
        print("  ERROR: No se encontró header cod_mp_sistema en BD_MP_SISTEMA")
        return {}

    headers = [h.strip() for h in values[header_row_idx]]
    rows = values[header_row_idx + 1 :]

    try:
        _mp_stock_col_idx = headers.index("stock_actual") + 1
    except ValueError:
        _mp_stock_col_idx = None
        print("  WARN: columna stock_actual no encontrada en BD_MP_SISTEMA")

    result: dict[tuple[str, str], dict] = {}
    _mp_row_index.clear()
    for i, row in enumerate(rows):
        if not any(c.strip() for c in row):
            continue
        r = {
            headers[j]: row[j].strip()
            for j in range(min(len(headers), len(row)))
        }
        cod = r.get("cod_mp_sistema", "").strip()
        bod = normalizar_cod_bodega(r.get("cod_bodega", ""))
        if cod and bod:
            k = _mp_key(cod, bod)
            result[k] = r
            _mp_row_index[k] = header_row_idx + i + 2

    print(f"  {len(result)} filas MP×bodega cargadas")
    _mp_cache = result
    return result


def actualizar_stocks_sheets_batch(claves_a_stock: dict[tuple[str, str], float]):
    """batch_update por (cod_mp, cod_bodega)."""
    if not claves_a_stock:
        return
    col_idx = _mp_stock_col_idx
    if not col_idx:
        print("  WARN: columna stock_actual desconocida, no se actualiza Sheets")
        return

    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    data: list[dict] = []
    for key, nuevo_stock in claves_a_stock.items():
        row_idx = _mp_row_index.get(key)
        if not row_idx:
            print(f"  WARN: no fila MP×bodega {key[0]} @ {key[1]}")
            continue
        rng = rowcol_to_a1(row_idx, col_idx)
        data.append({"range": rng, "values": [[round(float(nuevo_stock), 4)]]})

    if not data:
        return
    ws.batch_update(data, value_input_option=ValueInputOption.user_entered)


_lookup_descargo = None


def _ensure_lookup_descargo() -> dict:
    global _lookup_descargo
    if _lookup_descargo is None:
        _lookup_descargo = construir_lookup(cargar_bd_productos())
    return _lookup_descargo


_sin_descargo_cache: set[str] | None = None


def _cargar_sin_descargo() -> set[str]:
    """cod_smart_menu marcados con descarga_inventario=NO en BD_PRODUCTOS."""
    global _sin_descargo_cache
    if _sin_descargo_cache is not None:
        return _sin_descargo_cache
    productos = cargar_bd_productos()
    _sin_descargo_cache = {
        (p.get("cod_smart_menu") or "").strip()
        for p in productos
        if str(p.get("descarga_inventario", "SI")).strip().upper() == "NO"
        and (p.get("cod_smart_menu") or "").strip()
    }
    muestra = sorted(_sin_descargo_cache)[:40]
    suf = " …" if len(_sin_descargo_cache) > 40 else ""
    print(
        f"  {len(_sin_descargo_cache)} cod_smart_menu sin descargo inventario "
        f"(descarga_inventario=NO): {muestra}{suf}"
    )
    return _sin_descargo_cache


def _cod_smart_menu_venta(venta: dict) -> str:
    cod_sm = (venta.get("cod_smart_menu") or "").strip()
    if not cod_sm:
        cod_sm = (venta.get("cod_producto") or "").strip()
    return cod_sm


def _resolver_cod_receta(venta: dict) -> str | None:
    """
    Preferir columna cod_receta en hist_ventas; si falta (filas viejas),
    derivar desde cod_smart_menu + variedad vía BD_PRODUCTOS.
    Nota: cod_producto en hist_ventas suele ser el mismo valor que cod_smart_menu.
    """
    direct = (venta.get("cod_receta") or "").strip()
    if direct:
        return direct

    cod_sm = _cod_smart_menu_venta(venta)
    if not cod_sm:
        return None

    variedad = (venta.get("variedad_smart_menu") or "").strip()
    m = resolver_match(cod_sm, variedad, _ensure_lookup_descargo())
    return (m.get("cod_receta") or "").strip() or None


def _cod_receta_desde_catalogo(venta: dict) -> str | None:
    """
    cod_receta según BD_PRODUCTOS actual, ignorando la columna cod_receta en hist_ventas.
    Sirve cuando la venta se insertó con catálogo viejo y ya corrigieron la hoja.
    """
    cod_sm = _cod_smart_menu_venta(venta)
    if not cod_sm:
        return None
    detalle = (venta.get("variedad_smart_menu") or "").strip()
    m = resolver_match(cod_sm, detalle, _ensure_lookup_descargo())
    return (m.get("cod_receta") or "").strip() or None


# ── CALCULAR CONSUMO ──────────────────────────────────────────
def calcular_consumo(ingrediente: dict, cantidad_vendida: float) -> float:
    try:
        gramaje = float(ingrediente.get("cantidad", 0))
        pct_aplicacion = float(ingrediente.get("pct_aplicacion", 1) or 1)
        merma_pct = float(ingrediente.get("merma_pct", 0) or 0)
    except ValueError:
        return 0.0

    return cantidad_vendida * gramaje * pct_aplicacion * (1 + merma_pct)


# ── PROCESAR DESCARGO ─────────────────────────────────────────
def _fecha_etiqueta_alerta(fecha_filtro: str | None, sin_receta: list[dict]) -> str:
    """Texto para cabecera WA: día filtrado o resumen de fechas en los ítems."""
    if fecha_filtro:
        return fecha_filtro.strip()
    fs = sorted(
        {
            str(x.get("fecha") or "").strip()
            for x in sin_receta
            if str(x.get("fecha") or "").strip()
        }
    )
    if not fs:
        return "varias fechas"
    if len(fs) == 1:
        return fs[0]
    if len(fs) <= 3:
        return ", ".join(fs)
    return f"{', '.join(fs[:3])} (+{len(fs) - 3} días más)"


def procesar_descargo(fecha: str | None = None) -> dict:
    query = (
        supabase.table("hist_ventas")
        .select("*")
        .eq("estado_match", "PROCESADO")
        .eq("descargado", False)
    )
    if fecha:
        query = query.eq("fecha", fecha)

    ventas = query.execute().data
    print(f"  {len(ventas)} ventas pendientes de descargo")

    sin_receta: list[dict] = []

    if not ventas:
        return {"sin_receta": []}

    # Cargar documentos ya descargados en mov_inventario para evitar duplicados
    print("  Cargando documentos ya descargados en mov_inventario...")
    ya_en_mov: set[str] = set()
    fechas_unicas = list({str(v.get("fecha", "")) for v in ventas if v.get("fecha")})
    for f in fechas_unicas:
        desde = f"{f}T00:00:00"
        hasta = f"{f}T23:59:59"
        offset = 0
        while True:
            chunk = (
                supabase.table("mov_inventario")
                .select("num_documento")
                .eq("tipo_mov", "SALIDA_VENTA")
                .eq("origen_documento", "VENTA_SMART_MENU")
                .gte("fecha", desde)
                .lte("fecha", hasta)
                .range(offset, offset + 999)
                .execute()
                .data
                or []
            )
            ya_en_mov.update(
                r.get("num_documento")
                for r in chunk
                if r.get("num_documento")
            )
            if len(chunk) < 1000:
                break
            offset += 1000
    print(f"  {len(ya_en_mov)} documentos ya en mov_inventario (se omitirán)")

    mp_sistema = cargar_mp_sistema()
    sin_descargo = _cargar_sin_descargo()
    incluir_sub = descargo_subrecetas_habilitado()
    subs_meta: dict[str, dict] = {}
    if incluir_sub:
        subs_meta = cargar_metadata_subrecetas()
        print(
            f"  Descargo SUB activo: {len(subs_meta)} subrecetas "
            f"(pseudo-MP {PREFIJO_PSEUDO_MP}* en BD_MP_SISTEMA)"
        )
    else:
        print("  Descargo SUB desactivado (DESCARGO_SUBRECETAS=0, solo líneas MP)")
    movimientos_ok = 0
    movimientos_err = 0
    salidas_mp = 0
    salidas_sub = 0
    stocks_actualizados: set[tuple[str, str]] = set()
    alertas_stock_negativo: list[dict] = []

    for venta in ventas:
        cod_venta = venta.get("cod_venta")
        cod_sm_venta = _cod_smart_menu_venta(venta)
        if cod_sm_venta in sin_descargo:
            try:
                supabase.table("hist_ventas").update(
                    {"descargado": True, "fecha_descargo": datetime.now().isoformat()}
                ).eq("cod_venta", cod_venta).execute()
            except Exception as e:
                print(f"  WARN: marcar descargado (descarga_inventario=NO) {cod_venta}: {e}")
            continue
        if cod_venta in ya_en_mov:
            try:
                supabase.table("hist_ventas").update(
                    {"descargado": True, "fecha_descargo": datetime.now().isoformat()}
                ).eq("cod_venta", cod_venta).execute()
            except Exception as e:
                print(f"  WARN: marcar descargado (ya en mov) {cod_venta}: {e}")
            continue
        fecha_v = venta.get("fecha")
        estado_doc = venta.get("estado_documento")
        if estado_documento_excluye_neto_operativo(estado_doc):
            try:
                supabase.table("hist_ventas").update(
                    {
                        "descargado": True,
                        "fecha_descargo": datetime.now().isoformat(),
                    }
                ).eq("cod_venta", cod_venta).execute()
            except Exception as e:
                print(f"  WARN: marcar sin descargo como procesada {cod_venta}: {e}")
            motivo = (estado_doc or "ACTIVO").strip().upper()
            print(
                f"  INFO: documento {motivo} — sin descargo inventario ({cod_venta})"
            )
            continue

        cod_receta_hist = (venta.get("cod_receta") or "").strip()
        cod_receta = _resolver_cod_receta(venta)
        variedad = venta.get("variedad_smart_menu")
        cantidad_v = float(venta.get("cantidad_vendida", 1))

        if not cod_receta:
            print(f"  WARN: venta {cod_venta} sin cod_receta, skip")
            continue

        ingredientes_raw = get_ingredientes(cod_receta, variedad)
        lineas_mp, lineas_sub = preparar_ingredientes_descargo(
            ingredientes_raw, incluir_sub=incluir_sub
        )
        if not lineas_mp and not lineas_sub:
            cod_cat = _cod_receta_desde_catalogo(venta)
            if cod_cat and cod_cat != cod_receta:
                alt_raw = get_ingredientes(cod_cat, variedad)
                alt_mp, alt_sub = preparar_ingredientes_descargo(
                    alt_raw, incluir_sub=incluir_sub
                )
                if alt_mp or alt_sub:
                    print(
                        f"    INFO: hist cod_receta={cod_receta!r} sin ingredientes; "
                        f"usando BD_PRODUCTOS {cod_cat!r} ({cod_venta})"
                    )
                    cod_receta = cod_cat
                    lineas_mp, lineas_sub = alt_mp, alt_sub
        if not lineas_mp and not lineas_sub:
            print(
                f"    WARN: sin ingredientes para receta={cod_receta} variedad='{variedad}'"
            )
            cod_sm = (venta.get("cod_smart_menu") or venta.get("cod_producto") or "").strip()
            var_s = (variedad or "").strip() if isinstance(variedad, str) else str(variedad or "").strip()
            nombre_p = (venta.get("nombre_producto") or "").strip() or "(sin nombre)"
            sin_receta.append(
                {
                    "cod_smart_menu": cod_sm,
                    "variedad": var_s,
                    "nombre": nombre_p,
                    "fecha": str(fecha_v or "").strip(),
                }
            )
            continue

        movs: list[dict] = []
        deltas: list[tuple[str, str, float]] = []

        for ing in lineas_mp:
            cod_mp = ing.get("cod_mp_sistema", "").strip()
            if not cod_mp or cod_mp.startswith("#"):
                continue

            consumo = calcular_consumo(ing, cantidad_v)
            if consumo <= 0:
                continue

            # Fallback: primera fila cocina/barra del MP en maestro
            mp_fb = None
            for bod_try in ("BOD-001", "BOD-002"):
                mp_fb = mp_sistema.get(_mp_key(cod_mp, bod_try))
                if mp_fb:
                    break

            bodega, err_bod = resolver_bodega_receta(ing, mp_fb)
            if err_bod == "BODEGA_NO_DESCARGO":
                print(
                    f"    WARN: MP {cod_mp} receta con bodega no descargable "
                    f"({ing.get('cod_bodega')}) — solo cocina/barra; skip línea"
                )
                continue
            if err_bod or not bodega:
                print(
                    f"    WARN: MP {cod_mp} sin cod_bodega en receta (cocina/barra); skip"
                )
                continue

            mp_info = mp_sistema.get(_mp_key(cod_mp, bodega), mp_fb or {})
            unidad = mp_info.get("unidad_base", "") or ing.get("unidad_base", "")
            costo_u = _sheet_float(mp_info.get("costo_unitario_ref", 0) or 0)

            cod_mov = (
                f"MOV-{fecha_v.replace('-', '') if fecha_v else '00000000'}-{cod_mp}-"
                f"{uuid.uuid4().hex[:16]}"
            )

            mov = {
                "cod_mov": cod_mov,
                "fecha": _iso_fecha_hora_mov(fecha_v, venta.get("hora")),
                "tipo_mov": "SALIDA_VENTA",
                "cod_mp_sistema": cod_mp,
                "nombre_mp": ing.get("nombre_mp", "") or mp_info.get("nombre_mp", ""),
                "cod_bodega_origen": bodega,
                "cod_bodega_destino": None,
                "cantidad_mov": round(consumo, 4),
                "unidad_base": unidad,
                "costo_unitario": costo_u,
                "costo_total": round(consumo * costo_u, 4),
                "origen_documento": "VENTA_SMART_MENU",
                "num_documento": cod_venta,
                "registrado_por": "AGENTE",
                "observaciones": (
                    f"Descargo automático receta {cod_receta} var={variedad} bod={bodega}"
                ),
            }
            movs.append(mov)
            deltas.append((cod_mp, bodega, consumo))
            salidas_mp += 1

        for ing in lineas_sub:
            mov, delta, warn = procesar_linea_sub_venta(
                ing,
                cantidad_vendida=cantidad_v,
                cod_receta=cod_receta,
                variedad=variedad,
                cod_venta=cod_venta,
                fecha_v=fecha_v,
                hora_raw=venta.get("hora"),
                mp_sistema=mp_sistema,
                subs_meta=subs_meta,
                mp_key_fn=_mp_key,
                iso_fecha_hora_mov=_iso_fecha_hora_mov,
            )
            if warn:
                print(f"    WARN: {warn} ({cod_venta})")
                continue
            if mov and delta:
                movs.append(mov)
                deltas.append(delta)
                salidas_sub += 1

        venta_ok = True
        if movs:
            try:
                supabase.table("mov_inventario").insert(movs).execute()
                movimientos_ok += len(movs)
            except Exception as e:
                print(f"  ERROR insertando movs venta {cod_venta}: {e}")
                movimientos_err += len(movs)
                venta_ok = False
        else:
            pass

        if venta_ok:
            for cod_mp, bodega, consumo in deltas:
                k = _mp_key(cod_mp, bodega)
                if k in mp_sistema:
                    stock_actual = _sheet_float(mp_sistema[k].get("stock_actual") or 0)
                    nuevo = stock_actual - consumo
                    mp_sistema[k]["stock_actual"] = nuevo
                    stocks_actualizados.add(k)
                    if nuevo < -0.0001:
                        alertas_stock_negativo.append(
                            {
                                "cod_mp": cod_mp,
                                "nombre_mp": mp_sistema[k].get("nombre_mp", ""),
                                "cod_bodega": bodega,
                                "stock_restante": round(nuevo, 4),
                                "unidad": mp_sistema[k].get("unidad_base", ""),
                                "cod_venta": cod_venta,
                            }
                        )

            upd = {
                "descargado": True,
                "fecha_descargo": datetime.now().isoformat(),
            }
            if cod_receta_hist != (cod_receta or "").strip():
                upd["cod_receta"] = cod_receta
            supabase.table("hist_ventas").update(upd).eq("cod_venta", cod_venta).execute()

    if sin_receta:
        try:
            from alertas_pipeline import alerta_ventas_sin_receta

            alerta_ventas_sin_receta(
                sin_receta, _fecha_etiqueta_alerta(fecha, sin_receta)
            )
        except Exception as e:
            print(f"  WARN: alerta_ventas_sin_receta: {e}")

    if alertas_stock_negativo:
        try:
            from alertas_inventario_barra import alerta_wa_descargo_stock_negativo_barra
            from alertas_tatami import alerta_wa_descargo_stock_negativo

            alerta_wa_descargo_stock_negativo_barra(alertas_stock_negativo)
            alerta_wa_descargo_stock_negativo(alertas_stock_negativo)
        except Exception as e:
            print(f"  WARN: alerta descargo stock negativo: {e}")

    print(f"  Actualizando {len(stocks_actualizados)} filas MP×bodega en Sheets...")
    batch_stocks = {
        k: _sheet_float(mp_sistema[k].get("stock_actual") or 0)
        for k in stocks_actualizados
    }
    try:
        actualizar_stocks_sheets_batch(batch_stocks)
    except Exception as e:
        print(f"  ERROR actualizando stock en Sheets: {e}")
        print("  (mov_inventario y hist_ventas ya quedaron actualizados en Supabase)")

    print(f"\n  Movimientos insertados: {movimientos_ok}")
    print(f"    Salidas MP:  {salidas_mp}")
    print(f"    Salidas SUB: {salidas_sub}")
    print(f"  Movimientos con error:  {movimientos_err}")
    print(f"  MPs actualizados:       {len(stocks_actualizados)}")

    return {
        "sin_receta": sin_receta,
        "movimientos_ok": movimientos_ok,
        "movimientos_err": movimientos_err,
        "salidas_mp": salidas_mp,
        "salidas_sub": salidas_sub,
    }


def resetear_descargo_dia(fecha: str):
    """
    Rehace el descargo de un día:
    - Borra movimientos SALIDA_VENTA originados por Smart Menu en ese rango de fecha.
    - Resetea flags descargado/fecha_descargo en hist_ventas de ese día.
    """
    fecha = (fecha or "").strip()
    if not fecha:
        raise ValueError("fecha requerida (YYYY-MM-DD)")

    try:
        d = datetime.strptime(fecha, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("fecha invalida, use YYYY-MM-DD")

    desde = f"{fecha}T00:00:00"
    hasta = f"{(d + timedelta(days=1)).strftime('%Y-%m-%d')}T00:00:00"

    print("\n[0] Rehacer activado: borrando SALIDA_VENTA del día en mov_inventario...")
    try:
        res_del = (
            supabase.table("mov_inventario")
            .delete()
            .eq("tipo_mov", "SALIDA_VENTA")
            .eq("origen_documento", "VENTA_SMART_MENU")
            .gte("fecha", desde)
            .lt("fecha", hasta)
            .execute()
        )
        print(f"  -> movimientos borrados (aprox): {len(res_del.data or [])}")
    except Exception as e:
        print(f"  WARN no se pudo borrar mov_inventario del día: {e}")

    print("[0b] Reseteando flags descargado en hist_ventas del día...")
    try:
        supabase.table("hist_ventas").update(
            {"descargado": False, "fecha_descargo": None}
        ).eq("fecha", fecha).execute()
        print("  -> OK")
    except Exception as e:
        print(f"  WARN no se pudo resetear hist_ventas: {e}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Descargo inventario desde hist_ventas -> mov_inventario")
    p.add_argument("--fecha", help="YYYY-MM-DD (opcional; default=todas pendientes)", default=None)
    p.add_argument(
        "--rehacer",
        action="store_true",
        help="Borra SALIDA_VENTA del día y resetea descargado antes de descargar",
    )
    a = p.parse_args()

    fecha = (a.fecha or "").strip() or None

    print(f"\n{'=' * 50}")
    print(f"MODULO DESCARGO — {fecha or 'TODAS PENDIENTES'}")
    print(f"{'=' * 50}")

    print("\n[1] Cargando catálogos...")
    cargar_recetas()
    cargar_mp_sistema()

    if a.rehacer and fecha:
        resetear_descargo_dia(fecha)

    print("\n[2] Procesando descargo...")
    resumen = procesar_descargo(fecha)
    mov_err = int(resumen.get("movimientos_err") or 0)
    if mov_err > 0:
        print(
            f"\nERROR: descargo con {mov_err} movimiento(s) fallidos "
            "(pipeline debe recibir codigo != 0)"
        )
        raise SystemExit(1)

    print(f"\n{'=' * 50}")

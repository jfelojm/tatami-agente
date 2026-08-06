"""
Recepción física de compras SRI — solo proveedores Tipo=Barra.

Flujo:
  1. Pipeline SRI/Drive procesa XML (match + precios + pendientes).
  2. Si el proveedor es Barra y SRI_BARRA_REQUIERE_OK=1:
     NO crea ENTRADA; encola líneas en staging POR_RECIBIR_BARRA.
  3. Operador marca usuario_ok (+ opcional cantidad_recibida) → API → ENTRADA.
     Puede ser OK total, por líneas, o por cantidad parcial (el resto queda en cola).

Otros proveedores (cocina, etc.) siguen con ENTRADA automática.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

from staging_common import (
    batch_format,
    crear_hoja_si_no_existe,
    find_header_row,
    header_style,
    open_staging,
    sheets_api,
    staging_spreadsheet_id,
)

load_dotenv(override=True)
log = logging.getLogger(__name__)

SHEET_POR_RECIBIR = "POR_RECIBIR_BARRA"

HEADERS = [
    "fecha_cola",
    "num_factura",
    "fecha_factura",
    "ruc_proveedor",
    "razon_social",
    "cod_proveedor",
    "cod_item_xml",
    "descripcion",
    "cantidad",  # cantidad factura (pendiente en cola)
    "cantidad_recibida",  # opcional: vacío = toda la cantidad; parcial = solo esa
    "costo_unitario",
    "total_linea",
    "cod_mp_sistema",
    "nombre_mp",
    "unidad_base",
    "cod_bodega",
    "estado",  # POR_RECIBIR | RECIBIDA_OK | RECHAZADA
    "usuario_ok",
    "fecha_ok",
    "clave_linea",
]

ESTADO_POR_RECIBIR = "POR_RECIBIR"
ESTADO_OK = "RECIBIDA_OK"
ESTADO_RECHAZADA = "RECHAZADA"

_barra_cache: set[str] | None = None  # RUCs normalizados Tipo=Barra
_barra_cod_cache: set[str] | None = None


def barra_requiere_ok() -> bool:
    raw = (os.getenv("SRI_BARRA_REQUIERE_OK") or "1").strip().lower()
    return raw in ("1", "true", "yes", "si", "sí")


def _solo_digitos_ruc(ruc: str) -> str:
    return re.sub(r"\D+", "", (ruc or "").strip())


def _cargar_rucs_barra(*, force: bool = False) -> set[str]:
    """RUCs de proveedores activos con Tipo=Barra (BD_PROV maestro)."""
    global _barra_cache, _barra_cod_cache
    if _barra_cache is not None and not force:
        return _barra_cache

    from staging_common import open_master

    ws = open_master().worksheet("BD_PROV")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_proveedor")
    if hi is None:
        _barra_cache = set()
        _barra_cod_cache = set()
        return _barra_cache

    h = [(c or "").strip() for c in vals[hi]]
    icod = h.index("cod_proveedor")
    iruc = next((h.index(k) for k in ("RUC", "ruc") if k in h), None)
    itipo = next((h.index(k) for k in ("Tipo", "tipo") if k in h), None)
    iact = next((h.index(k) for k in ("activo", "Activo") if k in h), None)

    rucs: set[str] = set()
    cods: set[str] = set()
    for row in vals[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        if iact is not None and iact < len(row):
            if (row[iact] or "").strip().upper() == "NO":
                continue
        tipo = (row[itipo] if itipo is not None and itipo < len(row) else "").strip().upper()
        if "BARRA" not in tipo:
            continue
        cod = (row[icod] if icod < len(row) else "").strip()
        if cod:
            cods.add(cod)
        if iruc is not None and iruc < len(row):
            dig = _solo_digitos_ruc(row[iruc])
            if dig:
                rucs.add(dig)

    _barra_cache = rucs
    _barra_cod_cache = cods
    log.info("Proveedores Barra: %d RUC | %d códigos", len(rucs), len(cods))
    return _barra_cache


def es_proveedor_barra(ruc: str, cod_proveedor: str = "") -> bool:
    """True si el emisor es Tipo=Barra (por RUC o cod_proveedor)."""
    rucs = _cargar_rucs_barra()
    dig = _solo_digitos_ruc(ruc)
    if dig and dig in rucs:
        return True
    cod = (cod_proveedor or "").strip()
    if cod and _barra_cod_cache and cod in _barra_cod_cache:
        return True
    return False


def clave_linea_cola(num_factura: str, cod_item_xml: str) -> str:
    from codigo_factura_match import normalizar_cod_item_para_match

    n = (num_factura or "").strip()
    c = normalizar_cod_item_para_match(cod_item_xml or "")
    return f"{n}|{c}"


def _parse_qty(raw) -> float | None:
    """Lee cantidad/costo desde Sheets (punto o coma; corrige miles mal aplicados)."""
    from numeros_sheets import parse_numero_sheets

    s = str(raw or "").strip()
    if not s:
        return None
    v = parse_numero_sheets(s, default=float("nan"))
    if v != v:  # NaN
        return None
    return v


def _num_cola(v, ndigits: int = 6) -> float | str:
    """Escribe número como float RAW (evita 11.793.333 por locale es-EC)."""
    from numeros_sheets import numero_celda_sheets

    return numero_celda_sheets(v, ndigits=ndigits)


def setup_hoja_por_recibir(*, force_headers: bool = False) -> str:
    """Crea/actualiza POR_RECIBIR_BARRA en staging. Retorna spreadsheet id."""
    sid = staging_spreadsheet_id()
    sheets = sheets_api()
    sheet_id = crear_hoja_si_no_existe(sheets, sid, SHEET_POR_RECIBIR)
    stg = open_staging()
    ws = stg.worksheet(SHEET_POR_RECIBIR)
    vals = ws.get_all_values()
    need_headers = force_headers or not vals or not any(
        (c or "").strip() == "num_factura" for c in (vals[0] if vals else [])
    )
    if need_headers:
        ws.update(
            range_name="A1",
            values=[HEADERS],
            value_input_option=ValueInputOption.user_entered,
        )
        batch_format(sheets, sid, header_style(sheet_id, len(HEADERS)))
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
        log.info("Headers escritos en %s", SHEET_POR_RECIBIR)
    else:
        # Migrar columna cantidad_recibida si falta (sin borrar datos).
        headers = [(c or "").strip() for c in vals[0]]
        if "cantidad_recibida" not in headers and "cantidad" in headers:
            idx = headers.index("cantidad") + 1  # 0-based insert after cantidad
            nrows = max(len(vals), 1)
            ws.insert_cols([[""] * nrows], col=idx + 1)
            ws.update_cell(1, idx + 1, "cantidad_recibida")
            log.info("Columna cantidad_recibida insertada en %s", SHEET_POR_RECIBIR)
    return sid


def _leer_filas_cola() -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    stg = open_staging()
    try:
        ws = stg.worksheet(SHEET_POR_RECIBIR)
    except Exception:
        setup_hoja_por_recibir()
        ws = open_staging().worksheet(SHEET_POR_RECIBIR)

    vals = ws.get_all_values()
    if not vals:
        return HEADERS, []
    hi = 0
    if not any((c or "").strip() == "num_factura" for c in vals[0]):
        for i, row in enumerate(vals[:5]):
            if any((c or "").strip() == "num_factura" for c in row):
                hi = i
                break
    headers = [(c or "").strip() for c in vals[hi]]
    filas: list[tuple[int, dict[str, str]]] = []
    for i in range(hi + 1, len(vals)):
        row = vals[i]
        if not any((c or "").strip() for c in row):
            continue
        d = {
            headers[j]: (row[j] if j < len(row) else "").strip()
            for j in range(len(headers))
            if headers[j]
        }
        filas.append((i + 1, d))
    return headers, filas


def lineas_ya_en_cola(num_factura: str) -> set[str]:
    """claves num_factura|cod_item ya en POR_RECIBIR o RECIBIDA_OK."""
    _, filas = _leer_filas_cola()
    out: set[str] = set()
    n = (num_factura or "").strip()
    for _, d in filas:
        if (d.get("num_factura") or "").strip() != n:
            continue
        est = (d.get("estado") or "").strip().upper()
        if est in (ESTADO_POR_RECIBIR, ESTADO_OK):
            k = (d.get("clave_linea") or "").strip()
            if not k:
                k = clave_linea_cola(n, d.get("cod_item_xml") or "")
            out.add(k)
    return out


def encolar_lineas_por_recibir(
    factura: dict,
    lineas: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> int:
    """
    Append líneas a POR_RECIBIR_BARRA (dedupe por clave_linea).
    Cada línea: cod_item_xml, descripcion, cantidad, costo_unitario, total_linea,
    cod_mp_sistema, nombre_mp, unidad_base, cod_bodega, cod_proveedor.
    """
    if not lineas:
        return 0
    setup_hoja_por_recibir()
    num = (factura.get("num_factura") or "").strip()
    ya = lineas_ya_en_cola(num)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ruc_fac = re.sub(r"\D", "", (factura.get("ruc") or "").strip())
    if len(ruc_fac) == 12 and not ruc_fac.startswith("0"):
        ruc_fac = "0" + ruc_fac
    nuevas: list[list] = []
    for ln in lineas:
        cod_xml = (ln.get("cod_item_xml") or "").strip()
        clave = clave_linea_cola(num, cod_xml)
        if clave in ya:
            continue
        ya.add(clave)
        nuevas.append(
            [
                now,
                num,
                (factura.get("fecha_factura") or "").strip()[:10],
                ruc_fac or (factura.get("ruc") or "").strip(),
                (factura.get("razon_social") or "").strip()[:80],
                (ln.get("cod_proveedor") or "").strip(),
                cod_xml,
                (ln.get("descripcion") or "").strip()[:120],
                _num_cola(ln.get("cantidad"), 4),
                "",  # cantidad_recibida (operador)
                _num_cola(ln.get("costo_unitario"), 6),
                _num_cola(ln.get("total_linea"), 4),
                (ln.get("cod_mp_sistema") or "").strip(),
                (ln.get("nombre_mp") or "").strip()[:60],
                (ln.get("unidad_base") or "").strip(),
                (ln.get("cod_bodega") or "").strip(),
                ESTADO_POR_RECIBIR,
                "",
                "",
                clave,
            ]
        )

    if not nuevas:
        print(f"  POR_RECIBIR_BARRA: 0 nuevas (ya en cola) factura {num}")
        return 0

    if dry_run:
        print(f"  [DRY RUN] POR_RECIBIR_BARRA: encolaría {len(nuevas)} línea(s)")
        return len(nuevas)

    ws = open_staging().worksheet(SHEET_POR_RECIBIR)
    vals = ws.get_all_values()
    start = len(vals) + 1
    # RAW + float: locale es-EC no interpreta el punto como miles
    ws.update(
        range_name=f"A{start}",
        values=nuevas,
        value_input_option=ValueInputOption.raw,
    )
    print(f"  POR_RECIBIR_BARRA: +{len(nuevas)} línea(s) factura {num}")
    return len(nuevas)


def listar_por_recibir(*, solo_pendientes: bool = True) -> list[dict]:
    _, filas = _leer_filas_cola()
    out = []
    for row_n, d in filas:
        est = (d.get("estado") or "").strip().upper()
        if solo_pendientes and est != ESTADO_POR_RECIBIR:
            continue
        out.append({"_row": row_n, **d})
    return out


def confirmar_factura_ok(
    num_factura: str,
    *,
    usuario: str = "",
    dry_run: bool = False,
    claves_linea: list[str] | None = None,
    cantidades_recibidas: dict[str, float] | None = None,
) -> dict:
    """
    OK de recepción: líneas POR_RECIBIR → ENTRADA inventario.

    - ``claves_linea``: si viene, solo esas líneas (OK parcial por ítem).
    - ``cantidades_recibidas``: {clave_linea: qty} opcional; si no, usa columna
      ``cantidad_recibida`` de la hoja; si vacía, toda ``cantidad``.
    - Si qty recibida < cantidad factura, deja resto en nueva fila POR_RECIBIR.
    """
    from procesar_facturas_drive import (
        calcular_entrada_desde_factura,
        cargar_bd_items_prov,
        conversion_compra_definida,
        registrar_entrada_inventario,
        _flush_mp_sistema,
        _mp_cache_key,
        _parse_factor_positivo,
        buscar_item_prov,
        invalidar_caches_facturas_inventario,
    )
    from inventario_stock_mp import norm_mp

    # Siempre datos frescos de Sheets (webhook Railway cachea en memoria)
    invalidar_caches_facturas_inventario()
    cargar_bd_items_prov()

    num = (num_factura or "").strip()
    if not num:
        return {"ok": False, "error": "num_factura vacío"}

    filtro_claves: set[str] | None = None
    if claves_linea:
        filtro_claves = {str(c).strip() for c in claves_linea if str(c).strip()}
        if not filtro_claves:
            filtro_claves = None

    qty_override: dict[str, float] = {}
    if cantidades_recibidas:
        for k, v in cantidades_recibidas.items():
            kk = str(k).strip()
            q = _parse_qty(v)
            if kk and q is not None and q > 0:
                qty_override[kk] = q

    headers, filas = _leer_filas_cola()
    pendientes = []
    for row_n, d in filas:
        if (d.get("num_factura") or "").strip() != num:
            continue
        if (d.get("estado") or "").strip().upper() != ESTADO_POR_RECIBIR:
            continue
        clave = (d.get("clave_linea") or "").strip() or clave_linea_cola(
            num, d.get("cod_item_xml") or ""
        )
        if filtro_claves is not None and clave not in filtro_claves:
            continue
        pendientes.append((row_n, d))

    if not pendientes:
        return {
            "ok": False,
            "error": (
                f"No hay líneas POR_RECIBIR seleccionadas para factura {num}"
                if filtro_claves
                else f"No hay líneas POR_RECIBIR para factura {num}"
            ),
            "num_factura": num,
            "parcial": bool(filtro_claves),
        }

    # Total POR_RECIBIR de la factura (antes de filtrar) para mensaje parcial
    total_pend_factura = sum(
        1
        for _, d in filas
        if (d.get("num_factura") or "").strip() == num
        and (d.get("estado") or "").strip().upper() == ESTADO_POR_RECIBIR
    )
    modo_parcial = filtro_claves is not None and len(pendientes) < total_pend_factura

    # Construir factura mínima + items (normalizar RUC 12→13 dígitos)
    sample = pendientes[0][1]
    ruc_raw = (sample.get("ruc_proveedor") or "").strip()
    ruc_digits = re.sub(r"\D", "", ruc_raw)
    if len(ruc_digits) == 12 and not ruc_digits.startswith("0"):
        ruc_digits = "0" + ruc_digits
    factura = {
        "num_factura": num,
        "fecha_factura": (sample.get("fecha_factura") or "").strip()[:10],
        "ruc": ruc_digits or ruc_raw,
        "razon_social": (sample.get("razon_social") or "").strip(),
    }

    entradas = 0
    errores: list[str] = []
    deltas_stock: dict[tuple[str, str], float] = {}
    deltas_costo: dict[tuple[str, str], float] = {}
    rows_ok: list[int] = []
    claves_ok: list[str] = []
    qtys_ok: list[dict] = []
    restos_rows: list[list] = []
    qty_parcial_lineas = 0
    cod_movs_creados: list[str] = []

    for row_n, d in pendientes:
        cant_fac = _parse_qty(d.get("cantidad")) or 0.0
        clave = (d.get("clave_linea") or "").strip() or clave_linea_cola(
            num, d.get("cod_item_xml") or ""
        )
        cant_rec = qty_override.get(clave)
        if cant_rec is None:
            cant_rec = _parse_qty(d.get("cantidad_recibida"))
        if cant_rec is None:
            cant_rec = cant_fac

        if cant_fac <= 0:
            errores.append(f"{d.get('cod_item_xml')}: cantidad factura inválida")
            continue
        if cant_rec <= 0:
            errores.append(f"{d.get('cod_item_xml')}: cantidad_recibida debe ser > 0")
            continue
        if cant_rec > cant_fac + 1e-9:
            errores.append(
                f"{d.get('cod_item_xml')}: cantidad_recibida ({cant_rec}) > "
                f"cantidad factura ({cant_fac})"
            )
            continue

        resto = round(cant_fac - cant_rec, 6)
        if resto > 1e-9:
            qty_parcial_lineas += 1
            modo_parcial = True

        costo_u_fac = _parse_qty(d.get("costo_unitario")) or 0.0
        total_rec = round(cant_rec * costo_u_fac, 4) if costo_u_fac else (
            _parse_qty(d.get("total_linea")) or 0.0
        )
        if cant_fac > 0 and (_parse_qty(d.get("total_linea")) or 0) > 0 and costo_u_fac <= 0:
            total_rec = round(
                (_parse_qty(d.get("total_linea")) or 0) * (cant_rec / cant_fac), 4
            )

        item_factura = {
            "cod_item_xml": d.get("cod_item_xml") or "",
            "descripcion_proveedor": d.get("descripcion") or "",
            "cantidad": cant_rec,
            "costo_efectivo": costo_u_fac
            if costo_u_fac > 0
            else (
                (_parse_qty(d.get("total_linea")) or 0) / cant_fac if cant_fac else 0
            ),
            "precio_total_sin_impuesto": total_rec,
        }

        item_prov = buscar_item_prov(
            factura["ruc"],
            item_factura["cod_item_xml"],
            item_factura["descripcion_proveedor"],
            factura.get("razon_social", ""),
            num,
        )
        if not item_prov:
            # Fallback solo por MP+proveedor de la cola — NUNCA inventar factor=1
            cod_mp_cola = norm_mp(d.get("cod_mp_sistema"))
            cod_prov_cola = (d.get("cod_proveedor") or "").strip()
            for it in cargar_bd_items_prov():
                if norm_mp(it.get("cod_mp_sistema")) != cod_mp_cola:
                    continue
                cp = (it.get("cod_proveedor") or "").strip()
                if cod_prov_cola and cp.lstrip("0") != cod_prov_cola.lstrip("0"):
                    continue
                if _parse_factor_positivo(it.get("factor_conversion")):
                    item_prov = it
                    break
        if not item_prov:
            errores.append(
                f"{item_factura['cod_item_xml']}: sin match en BD_ITEMS_PROV "
                "(promover ítem / revisar factor antes de OK)"
            )
            continue
        if not _parse_factor_positivo(item_prov.get("factor_conversion")):
            errores.append(
                f"{item_factura['cod_item_xml']}: factor_conversion inválido en catálogo"
            )
            continue

        ok_conv, motivo = conversion_compra_definida(item_prov)
        if not ok_conv:
            errores.append(f"{item_factura['cod_item_xml']}: {motivo}")
            continue

        bodega = (d.get("cod_bodega") or item_prov.get("cod_bodega_destino") or "BOD-002").strip()
        item_mov = dict(item_factura)
        extra_qty = ""
        if resto > 1e-9:
            extra_qty = f" | QTY_PARCIAL:{cant_rec}/{cant_fac}"
        item_mov["descripcion_proveedor"] = (
            f"{item_factura['descripcion_proveedor']} | ORIGEN:RECEPCION_BARRA{extra_qty}"
        )

        if dry_run:
            factor = _parse_factor_positivo(item_prov.get("factor_conversion"))
            cant, _, _ = calcular_entrada_desde_factura(item_factura, item_prov, factor or 1)
            print(
                f"  [DRY RUN] ENTRADA {item_prov.get('cod_mp_sistema')} +{cant} @ {bodega}"
                + (f" (recibido {cant_rec} de {cant_fac})" if resto > 1e-9 else "")
                + f" factor={factor}"
            )
            entradas += 1
            rows_ok.append(row_n)
            claves_ok.append(clave)
            qtys_ok.append(
                {"clave": clave, "recibida": cant_rec, "factura": cant_fac, "resto": max(0, resto)}
            )
            continue

        cod_mov = registrar_entrada_inventario(
            item_prov, item_mov, factura, cod_bodega_destino=bodega
        )
        if not cod_mov:
            errores.append(f"{item_factura['cod_item_xml']}: fallo ENTRADA")
            continue
        cod_movs_creados.append(cod_mov)

        factor = _parse_factor_positivo(item_prov.get("factor_conversion"))
        assert factor is not None
        cantidad_base, costo_u, _ = calcular_entrada_desde_factura(
            item_factura, item_prov, factor
        )
        cod_mp = (item_prov.get("cod_mp_sistema") or "").strip()
        key = _mp_cache_key(cod_mp, bodega)
        deltas_stock[key] = deltas_stock.get(key, 0.0) + cantidad_base
        deltas_costo[key] = costo_u
        entradas += 1
        rows_ok.append(row_n)
        claves_ok.append(clave)
        qtys_ok.append(
            {"clave": clave, "recibida": cant_rec, "factura": cant_fac, "resto": max(0, resto)}
        )

        if resto > 1e-9:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            total_resto = round(resto * (item_factura["costo_efectivo"] or 0), 4)
            restos_rows.append(
                [
                    now,
                    num,
                    factura["fecha_factura"],
                    factura["ruc"],
                    factura["razon_social"][:80],
                    (d.get("cod_proveedor") or "").strip(),
                    (d.get("cod_item_xml") or "").strip(),
                    (d.get("descripcion") or "").strip()[:120],
                    _num_cola(resto, 4),
                    "",  # cantidad_recibida
                    _num_cola(d.get("costo_unitario"), 6),
                    _num_cola(total_resto, 4),
                    (d.get("cod_mp_sistema") or "").strip(),
                    (d.get("nombre_mp") or "").strip()[:60],
                    (d.get("unidad_base") or "").strip(),
                    bodega,
                    ESTADO_POR_RECIBIR,
                    "",
                    "",
                    clave,
                ]
            )

    if not dry_run and (deltas_stock or deltas_costo):
        flush = _flush_mp_sistema(deltas_stock, deltas_costo, force_reload=True)
        if not flush.get("ok"):
            # Revertir ENTRADAS para no dejar stock desfasado vs Sheets
            try:
                from supabase import create_client

                sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
                for cm in cod_movs_creados:
                    sb.table("mov_inventario").delete().eq("cod_mov", cm).execute()
                    print(f"  REVERTIDO mov {cm} (flush stock falló)")
            except Exception as e:
                print(f"  ERROR revirtiendo ENTRADAS tras fallo flush: {e}")
            return {
                "ok": False,
                "error": flush.get("error")
                or "No se pudo actualizar stock en BD_MP_SISTEMA",
                "num_factura": num,
                "entradas_revertidas": len(cod_movs_creados),
                "flush": flush,
                "errores": errores,
            }

    # Marcar filas OK + cantidad_recibida efectiva (solo si stock quedó actualizado)
    if not dry_run and rows_ok:
        i_est = headers.index("estado") if "estado" in headers else None
        i_usr = headers.index("usuario_ok") if "usuario_ok" in headers else None
        i_fh = headers.index("fecha_ok") if "fecha_ok" in headers else None
        i_crec = (
            headers.index("cantidad_recibida") if "cantidad_recibida" in headers else None
        )
        ws = open_staging().worksheet(SHEET_POR_RECIBIR)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates = []
        qty_by_row = {}
        for i, row_n in enumerate(rows_ok):
            if i < len(qtys_ok):
                qty_by_row[row_n] = qtys_ok[i].get("recibida")
        for row_n in rows_ok:
            if i_est is not None:
                updates.append(
                    {"range": rowcol_to_a1(row_n, i_est + 1), "values": [[ESTADO_OK]]}
                )
            if i_usr is not None:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_n, i_usr + 1),
                        "values": [[(usuario or "sheets").strip()]],
                    }
                )
            if i_fh is not None:
                updates.append(
                    {"range": rowcol_to_a1(row_n, i_fh + 1), "values": [[now]]}
                )
            if i_crec is not None and row_n in qty_by_row:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_n, i_crec + 1),
                        "values": [[_num_cola(qty_by_row[row_n], 4)]],
                    }
                )
        if updates:
            ws.batch_update(updates, value_input_option=ValueInputOption.raw)

        if restos_rows:
            # Alinear columnas al header actual de la hoja
            vals = ws.get_all_values()
            start = len(vals) + 1
            # Si la hoja aún no tiene cantidad_recibida, setup ya debió migrar;
            # restos_rows sigue el orden HEADERS canónico.
            if headers == HEADERS or (
                "cantidad_recibida" in headers and len(headers) >= len(HEADERS)
            ):
                ws.update(
                    range_name=f"A{start}",
                    values=restos_rows,
                    value_input_option=ValueInputOption.raw,
                )
            else:
                # Mapear por nombre de columna
                mapped = []
                for resto_vals in restos_rows:
                    by_h = dict(zip(HEADERS, resto_vals))
                    mapped.append([(by_h.get(h) if by_h.get(h) is not None else "") for h in headers])
                ws.update(
                    range_name=f"A{start}",
                    values=mapped,
                    value_input_option=ValueInputOption.raw,
                )
            print(f"  POR_RECIBIR_BARRA: +{len(restos_rows)} resto(s) qty parcial")

    quedan_n = 0
    # Actualizar facturas_procesadas
    if not dry_run and entradas > 0:
        try:
            from supabase import create_client

            sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
            ruc = factura["ruc"]
            existing = (
                sb.table("facturas_procesadas")
                .select("*")
                .eq("num_factura", num)
                .eq("ruc_proveedor", ruc)
                .limit(1)
                .execute()
                .data
                or []
            )
            meta = dict((existing[0].get("meta") if existing else {}) or {})
            hist = list(meta.get("recepcion_barra_oks") or [])
            hist.append(
                {
                    "at": datetime.now().isoformat(),
                    "usuario": usuario or "sheets",
                    "entradas": entradas,
                    "parcial": modo_parcial or bool(filtro_claves) or qty_parcial_lineas > 0,
                    "claves": claves_ok,
                    "cantidades": qtys_ok,
                }
            )
            meta["recepcion_barra_oks"] = hist[-20:]
            meta["recepcion_barra_ok"] = hist[-1]
            # Si aún quedan POR_RECIBIR de esta factura → sigue POR_RECIBIR
            quedan_n = sum(
                1
                for _, d in _leer_filas_cola()[1]
                if (d.get("num_factura") or "").strip() == num
                and (d.get("estado") or "").upper() == ESTADO_POR_RECIBIR
            )
            sin_match = int((existing[0].get("items_sin_match") if existing else 0) or 0)
            if quedan_n:
                nuevo_estado = ESTADO_POR_RECIBIR
            elif sin_match > 0:
                nuevo_estado = "PARCIAL"
            else:
                nuevo_estado = "COMPLETA"

            payload = {
                "num_factura": num,
                "ruc_proveedor": ruc,
                "fecha_factura": factura["fecha_factura"],
                "fecha_proceso": datetime.now().isoformat(),
                "items_procesados": entradas
                + int((existing[0].get("items_procesados") if existing else 0) or 0),
                "items_sin_match": sin_match,
                "estado": nuevo_estado,
                "meta": meta,
                "drive_file_id": (existing[0].get("drive_file_id") if existing else "")
                or "",
            }
            sb.table("facturas_procesadas").upsert(
                payload, on_conflict="num_factura,ruc_proveedor"
            ).execute()
        except Exception as e:
            log.warning("No se pudo actualizar facturas_procesadas: %s", e)
            errores.append(f"facturas_procesadas: {e}")
    elif dry_run:
        quedan_n = max(0, total_pend_factura - entradas) + len(restos_rows)

    if not dry_run and entradas > 0 and not errores:
        ok_flag = True
    elif not dry_run and entradas > 0:
        ok_flag = True  # parcial con avisos
    elif dry_run and entradas > 0:
        ok_flag = True
    else:
        ok_flag = False

    return {
        "ok": ok_flag,
        "num_factura": num,
        "entradas": entradas,
        "pendientes_previas": total_pend_factura,
        "lineas_ok": len(claves_ok),
        "quedan_por_recibir": quedan_n,
        "parcial": modo_parcial
        or bool(filtro_claves and quedan_n > 0)
        or qty_parcial_lineas > 0,
        "qty_parcial_lineas": qty_parcial_lineas,
        "restos_encolados": len(restos_rows),
        "claves_ok": claves_ok,
        "cantidades": qtys_ok,
        "errores": errores,
        "dry_run": dry_run,
        "staging": f"https://docs.google.com/spreadsheets/d/{staging_spreadsheet_id()}",
    }


def main_setup() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sid = setup_hoja_por_recibir(force_headers=True)
    print(f"OK hoja {SHEET_POR_RECIBIR} en staging: https://docs.google.com/spreadsheets/d/{sid}")
    _cargar_rucs_barra(force=True)
    print(f"RUCs Barra cargados: {len(_barra_cache or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_setup())

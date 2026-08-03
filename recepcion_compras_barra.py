"""
Recepción física de compras SRI — solo proveedores Tipo=Barra.

Flujo:
  1. Pipeline SRI/Drive procesa XML (match + precios + pendientes).
  2. Si el proveedor es Barra y SRI_BARRA_REQUIERE_OK=1:
     NO crea ENTRADA; encola líneas en staging POR_RECIBIR_BARRA.
  3. Operador confirma OK total en Sheets → API → ENTRADA + cierra factura.

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
    "cantidad",
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
    nuevas: list[list[str]] = []
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
                (factura.get("ruc") or "").strip(),
                (factura.get("razon_social") or "").strip()[:80],
                (ln.get("cod_proveedor") or "").strip(),
                cod_xml,
                (ln.get("descripcion") or "").strip()[:120],
                str(ln.get("cantidad") or ""),
                str(ln.get("costo_unitario") or ""),
                str(ln.get("total_linea") or ""),
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
    ws.update(
        range_name=f"A{start}",
        values=nuevas,
        value_input_option=ValueInputOption.user_entered,
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
) -> dict:
    """
    OK total: todas las líneas POR_RECIBIR de la factura → ENTRADA inventario.
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
    )

    num = (num_factura or "").strip()
    if not num:
        return {"ok": False, "error": "num_factura vacío"}

    headers, filas = _leer_filas_cola()
    pendientes = [
        (row_n, d)
        for row_n, d in filas
        if (d.get("num_factura") or "").strip() == num
        and (d.get("estado") or "").strip().upper() == ESTADO_POR_RECIBIR
    ]
    if not pendientes:
        return {
            "ok": False,
            "error": f"No hay líneas POR_RECIBIR para factura {num}",
            "num_factura": num,
        }

    # Construir factura mínima + items
    sample = pendientes[0][1]
    factura = {
        "num_factura": num,
        "fecha_factura": (sample.get("fecha_factura") or "").strip()[:10],
        "ruc": (sample.get("ruc_proveedor") or "").strip(),
        "razon_social": (sample.get("razon_social") or "").strip(),
    }

    entradas = 0
    errores: list[str] = []
    deltas_stock: dict[tuple[str, str], float] = {}
    deltas_costo: dict[tuple[str, str], float] = {}
    rows_ok: list[int] = []

    for row_n, d in pendientes:
        item_factura = {
            "cod_item_xml": d.get("cod_item_xml") or "",
            "descripcion_proveedor": d.get("descripcion") or "",
            "cantidad": float(str(d.get("cantidad") or "0").replace(",", ".") or 0),
            "costo_efectivo": float(
                str(d.get("costo_unitario") or "0").replace(",", ".") or 0
            ),
            "precio_total_sin_impuesto": float(
                str(d.get("total_linea") or "0").replace(",", ".") or 0
            ),
        }
        # Enriquecer total si falta
        if item_factura["precio_total_sin_impuesto"] <= 0:
            item_factura["precio_total_sin_impuesto"] = round(
                item_factura["cantidad"] * item_factura["costo_efectivo"], 4
            )

        item_prov = buscar_item_prov(
            factura["ruc"],
            item_factura["cod_item_xml"],
            item_factura["descripcion_proveedor"],
            factura.get("razon_social", ""),
            num,
        )
        if not item_prov:
            # Fallback desde fila cola
            item_prov = {
                "cod_mp_sistema": d.get("cod_mp_sistema") or "",
                "nombre_mp": d.get("nombre_mp") or "",
                "unidad_base_sistema": d.get("unidad_base") or "uni",
                "cod_bodega_destino": d.get("cod_bodega") or "BOD-002",
                "factor_conversion": "1",
                "unidad_compra": "uni",
                "cod_proveedor": d.get("cod_proveedor") or "",
            }
            # Intentar factor real del catálogo por MP
            for it in cargar_bd_items_prov():
                if (it.get("cod_mp_sistema") or "").strip() == item_prov["cod_mp_sistema"]:
                    if (it.get("cod_proveedor") or "").strip() == (
                        d.get("cod_proveedor") or ""
                    ).strip() or not d.get("cod_proveedor"):
                        item_prov = it
                        break

        ok_conv, motivo = conversion_compra_definida(item_prov)
        if not ok_conv:
            errores.append(f"{item_factura['cod_item_xml']}: {motivo}")
            continue

        bodega = (d.get("cod_bodega") or item_prov.get("cod_bodega_destino") or "BOD-002").strip()
        item_mov = dict(item_factura)
        item_mov["descripcion_proveedor"] = (
            f"{item_factura['descripcion_proveedor']} | ORIGEN:RECEPCION_BARRA"
        )

        if dry_run:
            factor = _parse_factor_positivo(item_prov.get("factor_conversion"))
            cant, _, _ = calcular_entrada_desde_factura(item_factura, item_prov, factor or 1)
            print(f"  [DRY RUN] ENTRADA {item_prov.get('cod_mp_sistema')} +{cant} @ {bodega}")
            entradas += 1
            rows_ok.append(row_n)
            continue

        ok = registrar_entrada_inventario(
            item_prov, item_mov, factura, cod_bodega_destino=bodega
        )
        if not ok:
            errores.append(f"{item_factura['cod_item_xml']}: fallo ENTRADA")
            continue

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

    if not dry_run and (deltas_stock or deltas_costo):
        _flush_mp_sistema(deltas_stock, deltas_costo)

    # Marcar filas OK
    if not dry_run and rows_ok:
        i_est = headers.index("estado") if "estado" in headers else None
        i_usr = headers.index("usuario_ok") if "usuario_ok" in headers else None
        i_fh = headers.index("fecha_ok") if "fecha_ok" in headers else None
        ws = open_staging().worksheet(SHEET_POR_RECIBIR)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates = []
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
        if updates:
            ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)

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
            meta["recepcion_barra_ok"] = {
                "at": datetime.now().isoformat(),
                "usuario": usuario or "sheets",
                "entradas": entradas,
            }
            # Si aún quedan POR_RECIBIR de esta factura → sigue POR_RECIBIR
            quedan = [
                1
                for _, d in _leer_filas_cola()[1]
                if (d.get("num_factura") or "").strip() == num
                and (d.get("estado") or "").upper() == ESTADO_POR_RECIBIR
            ]
            sin_match = int((existing[0].get("items_sin_match") if existing else 0) or 0)
            if quedan:
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
        "pendientes_previas": len(pendientes),
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

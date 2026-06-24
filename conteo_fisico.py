"""
Inventario físico cíclico — operaciones contra Supabase + BD_MP_SISTEMA (Sheets).

Flujo: crear-ciclo → snapshot → registrar-envio → aprobar → contabilizar → (opcional) recalcular_stock_sheets.

El stock de referencia y el delta de ajuste usan la suma de mov_inventario (build_stock_calculado),
no stock_actual de Sheets, para evitar desfases como MP 166 en conteo 29-may-2026.

Requiere variables de entorno como el resto del agente: SUPABASE_URL, SUPABASE_KEY,
GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import gspread
from dotenv import load_dotenv
from supabase import create_client

from config_sheets import delta_abs_tol_conteo
from recalcular_stock_sheets import _clave_stock, build_stock_calculado
from google_credentials import google_credentials

load_dotenv(override=True)


def stock_mov_mp_bodega(
    stock_map: dict[tuple[str, str], float],
    cod_mp: str,
    cod_bodega: str,
) -> float:
    """Saldo en mov_inventario para (mp, bodega); 0 si no hay movimientos."""
    return float(stock_map.get(_clave_stock(cod_mp, cod_bodega), 0.0))


def delta_conteo_vs_mov(conteo_fisico: float, stock_mov: float) -> float:
    """Delta de ajuste: conteo físico menos saldo oficial (suma mov_inventario)."""
    return round(conteo_fisico - stock_mov, 6)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ESTADOS_CICLO_PERMITEN_ENVIO = frozenset({"BORRADOR_CONTEO"})
ESTADOS_CICLO_BLOQUEAN = frozenset({"CONTABILIZADO", "ANULADO"})


def _sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _get_sheet():
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _sheet_float(v) -> float:
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, 0.0)


def _paginar(sb, tabla: str, select: str, filtros: list[tuple] | None = None) -> list[dict]:
    filtros = filtros or []
    rows: list[dict] = []
    offset = 0
    while True:
        q = sb.table(tabla).select(select)
        for op, col, val in filtros:
            q = getattr(q, op)(col, val)
        chunk = q.range(offset, offset + 999).execute().data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def _err(code: str, message: str, details: dict | None = None) -> None:
    payload = {"error": {"code": code, "message": message, "details": details or {}}}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(1)


def _load_bd_mp_por_bodega(cod_bodega: str) -> list[dict]:
    cod_bodega = (cod_bodega or "").strip()
    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()
    header_row_idx = next(
        (i for i, r in enumerate(values) if any(c.strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if header_row_idx is None:
        _err("SHEET_HEADER", "No se encontró fila header con cod_mp_sistema en BD_MP_SISTEMA")

    headers = [h.strip() for h in values[header_row_idx]]
    rows = values[header_row_idx + 1 :]
    try:
        i_bod = headers.index("cod_bodega")
    except ValueError:
        _err("SHEET_COLUMN", "Columna cod_bodega no encontrada en BD_MP_SISTEMA")

    out: list[dict] = []
    vistos: set[str] = set()
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        r = {headers[j]: row[j].strip() for j in range(min(len(headers), len(row)))}
        cod = r.get("cod_mp_sistema", "").strip()
        if not cod or cod.startswith("#"):
            continue
        if (r.get("cod_bodega") or "").strip() != cod_bodega:
            continue
        if cod in vistos:
            continue
        vistos.add(cod)
        out.append(r)
    return out


def cmd_crear_ciclo(args: argparse.Namespace) -> None:
    sb = _sb()
    row = {
        "anio": int(args.anio),
        "semana_iso": int(args.semana_iso),
        "cod_bodega": args.cod_bodega.strip(),
        "estado": "PLANIFICADO",
        "sheet_name": (args.sheet_name or "CONTEO").strip(),
        "notas": args.notas or None,
    }
    if args.area_etiqueta:
        row["area_etiqueta"] = args.area_etiqueta.strip()
    if args.spreadsheet_id:
        row["spreadsheet_id"] = args.spreadsheet_id.strip()
    if args.id_humano:
        row["id_humano"] = args.id_humano.strip()
    if args.responsable_nombre:
        row["responsable_nombre"] = args.responsable_nombre.strip()
    if args.responsable_contacto:
        row["responsable_contacto"] = args.responsable_contacto.strip()

    if not args.produccion:
        print("[DRY RUN] crearía conteo_ciclo:", json.dumps(row, ensure_ascii=False, indent=2))
        return

    try:
        res = sb.table("conteo_ciclo").insert(row).execute()
    except Exception as e:
        _err("DB_INSERT", str(e), {"tabla": "conteo_ciclo"})
    data = res.data
    created = data[0] if isinstance(data, list) and data else data
    print(json.dumps(created, ensure_ascii=False, indent=2, default=str))


def cmd_snapshot(args: argparse.Namespace) -> None:
    sb = _sb()
    ciclo_id = args.ciclo_id.strip()
    r = sb.table("conteo_ciclo").select("*").eq("id", ciclo_id).limit(1).execute()
    ciclo = (r.data or [None])[0]
    if not ciclo:
        _err("CICLO_NOT_FOUND", f"No existe ciclo id={ciclo_id}")

    estado = (ciclo.get("estado") or "").strip()
    if estado in ESTADOS_CICLO_BLOQUEAN:
        _err("CICLO_WRONG_STATE", f"Ciclo en estado {estado}, no se puede snapshot")

    ex_ct = (
        sb.table("conteo_linea")
        .select("id", count="exact")
        .eq("ciclo_id", ciclo_id)
        .limit(1)
        .execute()
    )
    n_exist = ex_ct.count if ex_ct.count is not None else len(ex_ct.data or [])
    if n_exist and not args.reemplazar:
        _err(
            "SNAPSHOT_EXISTS",
            f"Ya hay {n_exist} líneas para este ciclo. Use --reemplazar para borrarlas y volver a cargar.",
        )

    cod_bodega = (ciclo.get("cod_bodega") or "").strip()
    mps = _load_bd_mp_por_bodega(cod_bodega)
    if not mps:
        _err("SNAPSHOT_EMPTY", f"Sin filas en BD_MP_SISTEMA para cod_bodega={cod_bodega}")

    now = datetime.now(timezone.utc).isoformat()
    stock_map = build_stock_calculado()
    lineas: list[dict] = []
    for i, r in enumerate(mps, start=1):
        cod_mp = (r.get("cod_mp_sistema") or "").strip()
        stock_mov = stock_mov_mp_bodega(stock_map, cod_mp, cod_bodega)
        lineas.append(
            {
                "ciclo_id": ciclo_id,
                "line_no": i,
                "cod_mp_sistema": cod_mp,
                "cod_bodega": cod_bodega,
                "nombre_mp": (r.get("nombre_mp") or "").strip() or None,
                "unidad_base": (r.get("unidad_base") or "").strip() or None,
                "stock_sistema_snapshot": round(stock_mov, 6),
                "costo_unitario_ref_snapshot": round(_sheet_float(r.get("costo_unitario_ref")), 8)
                if r.get("costo_unitario_ref")
                else None,
                "snapshot_at": now,
                "conteo_fisico": None,
            }
        )

    if not args.produccion:
        print(f"[DRY RUN] eliminaría {n_exist} líneas previas" if n_exist else "[DRY RUN] sin líneas previas")
        print(f"[DRY RUN] insertaría {len(lineas)} conteo_linea; estado ciclo -> BORRADOR_CONTEO")
        print("Ejemplo primera línea:", json.dumps(lineas[0], ensure_ascii=False, indent=2, default=str))
        return

    if n_exist:
        sb.table("conteo_linea").delete().eq("ciclo_id", ciclo_id).execute()

    batch = 200
    for off in range(0, len(lineas), batch):
        sb.table("conteo_linea").insert(lineas[off : off + batch]).execute()

    sb.table("conteo_ciclo").update(
        {
            "snapshot_at": now,
            "estado": "BORRADOR_CONTEO",
        }
    ).eq("id", ciclo_id).execute()

    print(f"OK: {len(lineas)} líneas snapshot para bodega {cod_bodega}; ciclo en BORRADOR_CONTEO.")


def _cargar_lineas_ciclo(sb, ciclo_id: str) -> list[dict]:
    return _paginar(sb, "conteo_linea", "*", [("eq", "ciclo_id", ciclo_id)])


def _norm_conteo_mp_cod(cod: object) -> str:
    """Clave de matching: cod_mp con zero-pad (p. ej. Sheets '5' vs JSON '005')."""
    s = str(cod or "").strip()
    return s.zfill(3) if s else ""


def _canonical_payload_hash(body: dict) -> str:
    s = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class ConteoRegistrarError(Exception):
    """Errores de validación / negocio al registrar envío (HTTP API o CLI)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict | None = None,
        *,
        http_status: int = 422,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status


def registrar_envio_desde_payload(
    sb,
    ciclo_id: str,
    body: dict,
    *,
    idempotency_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Misma lógica que registrar-envio CLI. Retorna dict (incl. idempotent_hit).
    Lanza ConteoRegistrarError ante errores de validación o estado.
    """
    ciclo_id = (ciclo_id or "").strip()
    r = sb.table("conteo_ciclo").select("*").eq("id", ciclo_id).limit(1).execute()
    ciclo = (r.data or [None])[0]
    if not ciclo:
        raise ConteoRegistrarError(
            "CICLO_NOT_FOUND",
            f"No existe ciclo id={ciclo_id}",
            http_status=404,
        )

    estado = (ciclo.get("estado") or "").strip()
    if estado in ESTADOS_CICLO_BLOQUEAN:
        raise ConteoRegistrarError(
            "CICLO_WRONG_STATE",
            f"Ciclo en estado {estado}, no admite envíos",
        )
    if estado not in ESTADOS_CICLO_PERMITEN_ENVIO:
        raise ConteoRegistrarError(
            "CICLO_WRONG_STATE",
            f"Estado {estado}: se esperaba BORRADOR_CONTEO (ejecute snapshot primero).",
        )

    sheet_id_c = (ciclo.get("spreadsheet_id") or "").strip()
    sheet_name_c = (ciclo.get("sheet_name") or "").strip() or "CONTEO"
    if sheet_id_c and (body.get("spreadsheet_id") or "").strip() != sheet_id_c:
        raise ConteoRegistrarError(
            "VALIDATION_SHEET_MISMATCH",
            "spreadsheet_id del JSON no coincide con conteo_ciclo",
            {"esperado": sheet_id_c, "recibido": body.get("spreadsheet_id")},
        )
    if (body.get("sheet_name") or "").strip() and (body.get("sheet_name") or "").strip() != sheet_name_c:
        raise ConteoRegistrarError(
            "VALIDATION_SHEET_MISMATCH",
            "sheet_name del JSON no coincide con conteo_ciclo",
            {"esperado": sheet_name_c, "recibido": body.get("sheet_name")},
        )

    lines_in = body.get("lines")
    if not lines_in or not isinstance(lines_in, list):
        raise ConteoRegistrarError("VALIDATION_LINES_EMPTY", "lines debe ser un arreglo no vacío")

    lineas_db = _cargar_lineas_ciclo(sb, ciclo_id)
    if not lineas_db:
        raise ConteoRegistrarError(
            "SNAPSHOT_NOT_READY",
            "No hay filas en conteo_linea; ejecute snapshot primero",
        )

    clave_db = {
        (_norm_conteo_mp_cod(x["cod_mp_sistema"]), x["cod_bodega"].strip()) for x in lineas_db
    }
    clave_payload: set[tuple[str, str]] = set()
    seen_dup: set[tuple[str, str]] = set()

    for i, L in enumerate(lines_in):
        cod_raw = str(L.get("cod_mp_sistema", "")).strip()
        bod_raw = str(L.get("cod_bodega", "")).strip()
        if not cod_raw or not bod_raw:
            raise ConteoRegistrarError(
                "VALIDATION_CONTEO_REQUIRED",
                f"Línea {i}: cod_mp_sistema y cod_bodega obligatorios",
            )
        cmp_ = (_norm_conteo_mp_cod(cod_raw), bod_raw)
        if cmp_ in seen_dup:
            raise ConteoRegistrarError("VALIDATION_DUPLICATE_KEY", f"Duplicado en payload: {cmp_}")
        seen_dup.add(cmp_)
        clave_payload.add(cmp_)

        cf = L.get("conteo_fisico")
        if cf is None or (isinstance(cf, str) and not str(cf).strip()):
            raise ConteoRegistrarError(
                "VALIDATION_CONTEO_REQUIRED",
                f"Línea {i}: conteo_fisico obligatorio (0 es válido)",
            )
        try:
            float(cf)
        except (TypeError, ValueError):
            raise ConteoRegistrarError(
                "VALIDATION_CONTEO_REQUIRED",
                f"Línea {i}: conteo_fisico debe ser numérico",
            )

    extra = clave_payload - clave_db
    if extra:
        raise ConteoRegistrarError(
            "VALIDATION_UNKNOWN_LINE",
            "El payload incluye MP+bodega que no pertenecen al ciclo",
            {"sobran": [list(x) for x in sorted(extra)][:50]},
        )

    idem = (idempotency_key or "").strip() or (body.get("idempotency_key") or "").strip()
    payload_hash = _canonical_payload_hash(body)

    if idem:
        prev = (
            sb.table("conteo_envio")
            .select("id,secuencia,estado_aprobacion,payload_hash,meta")
            .eq("ciclo_id", ciclo_id)
            .execute()
            .data
            or []
        )
        for e in prev:
            meta = e.get("meta") or {}
            if isinstance(meta, dict) and meta.get("idempotency_key") == idem:
                return {
                    "envio_id": e["id"],
                    "ciclo_id": ciclo_id,
                    "secuencia": e["secuencia"],
                    "lineas_persistidas": len(lines_in),
                    "payload_hash": e.get("payload_hash"),
                    "estado_aprobacion": e["estado_aprobacion"],
                    "idempotent_hit": True,
                }

    linea_by_key = {
        (_norm_conteo_mp_cod(x["cod_mp_sistema"]), x["cod_bodega"].strip()): x for x in lineas_db
    }

    if dry_run:
        return {
            "dry_run": True,
            "payload_hash": payload_hash,
            "lineas_validadas": len(lines_in),
            "mensaje": "Sin persistencia (dry run)",
        }

    prev_max = (
        sb.table("conteo_envio")
        .select("secuencia")
        .eq("ciclo_id", ciclo_id)
        .order("secuencia", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    secuencia = (prev_max[0]["secuencia"] + 1) if prev_max else 1

    meta: dict[str, Any] = {}
    if idem:
        meta["idempotency_key"] = idem

    envio_row = {
        "ciclo_id": ciclo_id,
        "secuencia": secuencia,
        "enviado_por": (body.get("enviado_por") or "").strip() or None,
        "enviado_por_contacto": (body.get("enviado_por_contacto") or "").strip() or None,
        "payload_hash": payload_hash,
        "observaciones": (body.get("observaciones") or "").strip() or None,
        "estado_aprobacion": "PENDIENTE_REVISION",
        "meta": meta,
    }
    try:
        ins = sb.table("conteo_envio").insert(envio_row).execute()
    except Exception as e:
        raise ConteoRegistrarError("DB_INSERT", str(e), {"tabla": "conteo_envio"}, http_status=500) from e
    envio = (ins.data or [None])[0]
    if not envio:
        raise ConteoRegistrarError("DB_INSERT", "No se pudo insertar conteo_envio", http_status=500)
    envio_id = envio["id"]

    stock_map = build_stock_calculado()
    detalles: list[dict] = []
    updates_linea: list[tuple[str, float, str | None]] = []

    for L in lines_in:
        key = (
            _norm_conteo_mp_cod(L.get("cod_mp_sistema", "")),
            str(L.get("cod_bodega", "")).strip(),
        )
        base = linea_by_key[key]
        cod_mp_db = (base.get("cod_mp_sistema") or "").strip()
        stock_mov = stock_mov_mp_bodega(stock_map, cod_mp_db, key[1])
        costo_snap = base.get("costo_unitario_ref_snapshot")
        costo_snap_f = float(costo_snap) if costo_snap is not None else None
        conteo_f = float(L["conteo_fisico"])
        delta = delta_conteo_vs_mov(conteo_f, stock_mov)
        valor_delta = delta * costo_snap_f if costo_snap_f is not None else None

        detalles.append(
            {
                "envio_id": envio_id,
                "line_no": L.get("line_no"),
                "cod_mp_sistema": cod_mp_db,
                "cod_bodega": key[1],
                "nombre_mp": base.get("nombre_mp"),
                "unidad_base": base.get("unidad_base"),
                "stock_sistema_snapshot": round(stock_mov, 6),
                "costo_unitario_ref_snapshot": costo_snap_f,
                "conteo_fisico": conteo_f,
                "delta_calculado": round(delta, 6),
                "valor_delta_estimado": round(valor_delta, 4) if valor_delta is not None else None,
                "notas": (L.get("notas") or "").strip() or None,
                "estado_linea": "PENDIENTE_APROBACION",
            }
        )
        updates_linea.append((cod_mp_db, conteo_f, (L.get("notas") or "").strip() or None))

    for off in range(0, len(detalles), 200):
        sb.table("conteo_envio_detalle").insert(detalles[off : off + 200]).execute()

    cod_bodega_ciclo = (ciclo.get("cod_bodega") or "").strip()
    for cod_mp, conteo_f, notas in updates_linea:
        upd: dict[str, Any] = {"conteo_fisico": conteo_f}
        if notas is not None:
            upd["notas"] = notas
        sb.table("conteo_linea").update(upd).eq("ciclo_id", ciclo_id).eq(
            "cod_mp_sistema", cod_mp
        ).eq("cod_bodega", cod_bodega_ciclo).execute()

    return {
        "envio_id": envio_id,
        "ciclo_id": ciclo_id,
        "secuencia": secuencia,
        "lineas_persistidas": len(detalles),
        "payload_hash": payload_hash,
        "estado_aprobacion": "PENDIENTE_REVISION",
    }


def cmd_registrar_envio(args: argparse.Namespace) -> None:
    sb = _sb()
    ciclo_id = args.ciclo_id.strip()

    if args.archivo:
        with open(args.archivo, encoding="utf-8") as f:
            body = json.load(f)
    else:
        body = json.load(sys.stdin)

    idem = (args.idempotency_key or "").strip() or None
    try:
        out = registrar_envio_desde_payload(
            sb,
            ciclo_id,
            body,
            idempotency_key=idem,
            dry_run=not args.produccion,
        )
    except ConteoRegistrarError as e:
        payload = {"error": {"code": e.code, "message": e.message, "details": e.details}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(1) from e

    if out.get("dry_run"):
        print("[DRY RUN] validación OK. Hash:", out.get("payload_hash"))
        n = int(out.get("lineas_validadas") or 0)
        print(
            f"[DRY RUN] insertaría conteo_envio + {n} detalle y actualizaría conteo_linea"
        )
        return

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


def cmd_aprobar(args: argparse.Namespace) -> None:
    sb = _sb()
    envio_id = args.envio_id.strip()
    r = sb.table("conteo_envio").select("*").eq("id", envio_id).limit(1).execute()
    envio = (r.data or [None])[0]
    if not envio:
        _err("ENVIO_NOT_FOUND", f"No existe envío id={envio_id}")

    est = (envio.get("estado_aprobacion") or "").strip()
    if est in {"CONTABILIZADO", "RECHAZADO"}:
        _err("ENVIO_WRONG_STATE", f"Envío en estado {est}")

    dets = _paginar(sb, "conteo_envio_detalle", "*", [("eq", "envio_id", envio_id)])
    if not dets:
        _err("ENVIO_EMPTY", "Sin detalle para este envío")

    solo = args.solo_cod_mp
    if solo:
        codigos = {x.strip() for x in solo.split(",") if x.strip()}
        for d in dets:
            if d["cod_mp_sistema"].strip() in codigos:
                if d["estado_linea"] == "PENDIENTE_APROBACION":
                    sb.table("conteo_envio_detalle").update({"estado_linea": "APROBADO"}).eq(
                        "id", d["id"]
                    ).execute()
    else:
        sb.table("conteo_envio_detalle").update({"estado_linea": "APROBADO"}).eq(
            "envio_id", envio_id
        ).eq("estado_linea", "PENDIENTE_APROBACION").execute()

    dets2 = _paginar(sb, "conteo_envio_detalle", "estado_linea", [("eq", "envio_id", envio_id)])
    states = [x["estado_linea"] for x in dets2]
    if any(s == "PENDIENTE_APROBACION" for s in states):
        nuevo_est = "PENDIENTE_REVISION"
    elif all(s == "APROBADO" for s in states):
        nuevo_est = "APROBADO_TOTAL"
    elif all(s == "RECHAZADO" for s in states):
        nuevo_est = "RECHAZADO"
    elif any(s == "APROBADO" for s in states) and any(s == "RECHAZADO" for s in states):
        nuevo_est = "APROBADO_PARCIAL"
    else:
        nuevo_est = "PENDIENTE_REVISION"

    now = datetime.now(timezone.utc).isoformat()
    sb.table("conteo_envio").update(
        {
            "estado_aprobacion": nuevo_est,
            "aprobado_at": now,
            "aprobado_por": args.por.strip(),
        }
    ).eq("id", envio_id).execute()

    print(f"OK: envío {envio_id} -> líneas actualizadas; estado_aprobacion={nuevo_est}")


def cmd_rechazar_todo(args: argparse.Namespace) -> None:
    sb = _sb()
    envio_id = args.envio_id.strip()
    now = datetime.now(timezone.utc).isoformat()
    sb.table("conteo_envio_detalle").update({"estado_linea": "RECHAZADO"}).eq(
        "envio_id", envio_id
    ).eq("estado_linea", "PENDIENTE_APROBACION").execute()
    sb.table("conteo_envio").update(
        {
            "estado_aprobacion": "RECHAZADO",
            "aprobado_at": now,
            "aprobado_por": args.por.strip(),
        }
    ).eq("id", envio_id).execute()
    print(f"OK: envío {envio_id} marcado RECHAZADO.")


def contabilizar_envio(
    sb,
    envio_id: str,
    *,
    registrado_por: str = "AGENTE_WA",
    cerrar_ciclo: bool = False,
    recalcular_sheets: bool = False,
    tol: float | None = None,
    produccion: bool = True,
) -> dict:
    """
    Versión importable de cmd_contabilizar.
    Retorna dict con movimientos_insertados, saltadas_umbral, advertencias.
    """
    envio_id = envio_id.strip()
    r = sb.table("conteo_envio").select("*").eq("id", envio_id).limit(1).execute()
    envio = (r.data or [None])[0]
    if not envio:
        raise ValueError(f"No existe envío id={envio_id}")

    est = (envio.get("estado_aprobacion") or "").strip()
    if est == "CONTABILIZADO":
        return {
            "envio_id": envio_id,
            "movimientos_insertados": 0,
            "saltadas_umbral": 0,
            "ya_contabilizado": True,
            "advertencias": [],
        }
    if est not in {"APROBADO_TOTAL", "APROBADO_PARCIAL"}:
        raise ValueError(
            f"Estado {est}: apruebe líneas antes de contabilizar "
            f"(APROBADO_TOTAL o APROBADO_PARCIAL)."
        )

    ciclo_id = envio["ciclo_id"]
    cr = sb.table("conteo_ciclo").select("*").eq("id", ciclo_id).limit(1).execute()
    ciclo = (cr.data or [None])[0]
    if not ciclo:
        raise ValueError(f"Ciclo huérfano {ciclo_id}")

    tol_f = delta_abs_tol_conteo(tol)
    dets = _paginar(sb, "conteo_envio_detalle", "*", [("eq", "envio_id", envio_id)])
    to_mov: list[dict] = []
    fecha_mov = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    saltadas_umbral = 0

    for d in dets:
        if d["estado_linea"] != "APROBADO":
            continue
        if d.get("cod_mov_ajuste"):
            continue
        delta = float(d["delta_calculado"])
        if abs(delta) < tol_f:
            saltadas_umbral += 1
            if produccion:
                sb.table("conteo_envio_detalle").update({"estado_linea": "CONTABILIZADO"}).eq(
                    "id", d["id"]
                ).execute()
            continue

        cod_mp = d["cod_mp_sistema"].strip()
        bodega = d["cod_bodega"].strip()
        nombre = (d.get("nombre_mp") or "").strip()
        unidad = (d.get("unidad_base") or "").strip()
        costo_u = d.get("costo_unitario_ref_snapshot")
        costo_f = float(costo_u) if costo_u is not None else 0.0
        cant = abs(delta)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]

        tipo = "AJUSTE_POSITIVO" if delta > 0 else "AJUSTE_NEGATIVO"
        cod_mov = (
            f"MOV-CONTEO+{envio_id[:8]}-{cod_mp}-{ts}"
            if delta > 0
            else f"MOV-CONTEO-{envio_id[:8]}-{cod_mp}-{ts}"
        )
        mov = {
            "cod_mov": cod_mov,
            "fecha": fecha_mov,
            "tipo_mov": tipo,
            "cod_mp_sistema": cod_mp,
            "nombre_mp": nombre,
            "cod_bodega_origen": None if delta > 0 else bodega,
            "cod_bodega_destino": bodega if delta > 0 else None,
            "cantidad_mov": round(cant, 4),
            "unidad_base": unidad,
            "costo_unitario": round(costo_f, 6) if costo_f else 0.0,
            "costo_total": round(cant * costo_f, 4) if costo_f else 0.0,
            "origen_documento": "CONTEO_FISICO",
            "num_documento": f"CONTEO-{ciclo_id[:8]}-E{envio['secuencia']}",
            "registrado_por": registrado_por,
            "observaciones": f"Ajuste inventario físico envio={envio_id} delta={delta:.4f}",
        }
        to_mov.append({"det_id": d["id"], "mov": mov})

    if not produccion:
        return {
            "dry_run": True,
            "envio_id": envio_id,
            "tol": tol_f,
            "movimientos_insertados": len(to_mov),
            "saltadas_umbral": saltadas_umbral,
            "muestra_movs": [
                {
                    "cod_mov": item["mov"]["cod_mov"],
                    "tipo_mov": item["mov"]["tipo_mov"],
                    "cantidad_mov": item["mov"]["cantidad_mov"],
                }
                for item in to_mov[:5]
            ],
        }

    for item in to_mov:
        ins = sb.table("mov_inventario").insert(item["mov"]).execute()
        row = (ins.data or [None])[0]
        cod_mov = (row or item["mov"])["cod_mov"]
        sb.table("conteo_envio_detalle").update(
            {"estado_linea": "CONTABILIZADO", "cod_mov_ajuste": cod_mov}
        ).eq("id", item["det_id"]).execute()

    sb.table("conteo_envio_detalle").update({"estado_linea": "CONTABILIZADO"}).eq(
        "envio_id", envio_id
    ).eq("estado_linea", "APROBADO").is_("cod_mov_ajuste", "null").execute()

    dets_f = _paginar(sb, "conteo_envio_detalle", "estado_linea", [("eq", "envio_id", envio_id)])
    estados_ok = {"CONTABILIZADO", "RECHAZADO"}
    if dets_f and all(x["estado_linea"] in estados_ok for x in dets_f):
        sb.table("conteo_envio").update(
            {
                "estado_aprobacion": "CONTABILIZADO",
                "contabilizado_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", envio_id).execute()

    advertencias: list[str] = []
    pend = [x for x in dets_f if x["estado_linea"] not in estados_ok]
    if pend:
        advertencias.append(f"{len(pend)} líneas aún pendientes de aprobación")

    if cerrar_ciclo:
        sb.table("conteo_ciclo").update({"estado": "CONTABILIZADO"}).eq("id", ciclo_id).execute()

    if recalcular_sheets:
        import subprocess

        here = os.path.dirname(os.path.abspath(__file__))
        py = os.path.join(here, "venv", "Scripts", "python.exe")
        if not os.path.exists(py):
            py = sys.executable
        subprocess.check_call(
            [py, os.path.join(here, "recalcular_stock_sheets.py"), "--produccion"]
        )

    return {
        "envio_id": envio_id,
        "movimientos_insertados": len(to_mov),
        "saltadas_umbral": saltadas_umbral,
        "advertencias": advertencias,
        "tol": tol_f,
    }


def cmd_contabilizar(args: argparse.Namespace) -> None:
    sb = _sb()
    try:
        out = contabilizar_envio(
            sb,
            args.envio_id.strip(),
            registrado_por=args.registrado_por.strip(),
            cerrar_ciclo=args.cerrar_ciclo,
            recalcular_sheets=args.recalcular_sheets,
            tol=args.tol,
            produccion=args.produccion,
        )
    except ValueError as e:
        msg = str(e)
        if msg.startswith("No existe envío"):
            _err("ENVIO_NOT_FOUND", msg)
        if "apruebe líneas" in msg:
            _err("ENVIO_NOT_APPROVED", msg)
        if msg.startswith("Ciclo huérfano"):
            _err("CICLO_NOT_FOUND", msg)
        _err("CONTABILIZAR", msg)

    if out.get("ya_contabilizado"):
        print("INFO: envío ya estaba CONTABILIZADO.")
        return

    if out.get("dry_run"):
        tol = float(out["tol"])
        print(
            f"[DRY RUN] umbral |delta| < {tol} -> sin movimiento (PARAMETROS/BD_CONFIG clave "
            f"conteo_delta_abs_tol, env CONTEO_DELTA_ABS_TOL, o --tol)"
        )
        print(
            f"[DRY RUN] generaría {out['movimientos_insertados']} mov_inventario; "
            f"{out['saltadas_umbral']} líneas sin ajuste por umbral; "
            "cerradas como CONTABILIZADO sin cod_mov_ajuste"
        )
        for m in out.get("muestra_movs") or []:
            print("  ", m["cod_mov"], m["tipo_mov"], m["cantidad_mov"])
        n = int(out["movimientos_insertados"])
        if n > 5:
            print(f"  ... +{n - 5} más")
        return

    for adv in out.get("advertencias") or []:
        print(f"WARN: {adv}")

    print(
        f"OK: contabilizado envío {out['envio_id']}; movimientos insertados: {out['movimientos_insertados']} "
        f"(umbral |delta|<{out['tol']}: {out['saltadas_umbral']} líneas sin mov)."
    )


class ConteoOperacionError(Exception):
    """Error de negocio en operaciones de conteo (API / WhatsApp)."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def crear_ciclo_api(
    *,
    anio: int,
    semana_iso: int,
    cod_bodega: str,
    sheet_name: str = "CONTEO",
    spreadsheet_id: str | None = None,
    area_etiqueta: str | None = None,
    id_humano: str | None = None,
    responsable_nombre: str | None = None,
    responsable_contacto: str | None = None,
    notas: str | None = None,
) -> dict:
    sb = _sb()
    row: dict[str, Any] = {
        "anio": int(anio),
        "semana_iso": int(semana_iso),
        "cod_bodega": (cod_bodega or "").strip(),
        "estado": "PLANIFICADO",
        "sheet_name": (sheet_name or "CONTEO").strip(),
        "spreadsheet_id": (spreadsheet_id or os.getenv("SPREADSHEET_ID") or "").strip() or None,
        "notas": notas,
    }
    if area_etiqueta:
        row["area_etiqueta"] = area_etiqueta.strip()
    if id_humano:
        row["id_humano"] = id_humano.strip()
    if responsable_nombre:
        row["responsable_nombre"] = responsable_nombre.strip()
    if responsable_contacto:
        row["responsable_contacto"] = responsable_contacto.strip()
    try:
        res = sb.table("conteo_ciclo").insert(row).execute()
    except Exception as e:
        raise ConteoOperacionError("DB_INSERT", str(e), {"tabla": "conteo_ciclo"}) from e
    data = res.data
    created = data[0] if isinstance(data, list) and data else data
    if not created:
        raise ConteoOperacionError("DB_INSERT", "No se devolvió el ciclo creado")
    return created


def snapshot_ciclo_api(ciclo_id: str, *, reemplazar: bool = False) -> dict:
    sb = _sb()
    ciclo_id = (ciclo_id or "").strip()
    r = sb.table("conteo_ciclo").select("*").eq("id", ciclo_id).limit(1).execute()
    ciclo = (r.data or [None])[0]
    if not ciclo:
        raise ConteoOperacionError("CICLO_NOT_FOUND", f"No existe ciclo id={ciclo_id}")

    estado = (ciclo.get("estado") or "").strip()
    if estado in ESTADOS_CICLO_BLOQUEAN:
        raise ConteoOperacionError("CICLO_WRONG_STATE", f"Ciclo en estado {estado}")

    ex_ct = (
        sb.table("conteo_linea")
        .select("id", count="exact")
        .eq("ciclo_id", ciclo_id)
        .limit(1)
        .execute()
    )
    n_exist = ex_ct.count if ex_ct.count is not None else len(ex_ct.data or [])
    if n_exist and not reemplazar:
        raise ConteoOperacionError(
            "SNAPSHOT_EXISTS",
            f"Ya hay líneas para este ciclo ({n_exist}). Use reemplazar=True.",
        )

    cod_bodega = (ciclo.get("cod_bodega") or "").strip()
    mps = _load_bd_mp_por_bodega(cod_bodega)
    if not mps:
        raise ConteoOperacionError(
            "SNAPSHOT_EMPTY", f"Sin MPs en BD_MP_SISTEMA para {cod_bodega}"
        )

    now = datetime.now(timezone.utc).isoformat()
    stock_map = build_stock_calculado()
    lineas: list[dict] = []
    for i, r in enumerate(mps, start=1):
        cod_mp = (r.get("cod_mp_sistema") or "").strip()
        stock_mov = stock_mov_mp_bodega(stock_map, cod_mp, cod_bodega)
        lineas.append(
            {
                "ciclo_id": ciclo_id,
                "line_no": i,
                "cod_mp_sistema": cod_mp,
                "cod_bodega": cod_bodega,
                "nombre_mp": (r.get("nombre_mp") or "").strip() or None,
                "unidad_base": (r.get("unidad_base") or "").strip() or None,
                "stock_sistema_snapshot": round(stock_mov, 6),
                "costo_unitario_ref_snapshot": round(
                    _sheet_float(r.get("costo_unitario_ref")), 8
                )
                if r.get("costo_unitario_ref")
                else None,
                "snapshot_at": now,
                "conteo_fisico": None,
            }
        )

    if n_exist:
        sb.table("conteo_linea").delete().eq("ciclo_id", ciclo_id).execute()

    batch = 200
    for off in range(0, len(lineas), batch):
        sb.table("conteo_linea").insert(lineas[off : off + batch]).execute()

    sb.table("conteo_ciclo").update(
        {"snapshot_at": now, "estado": "BORRADOR_CONTEO"}
    ).eq("id", ciclo_id).execute()

    return {
        "ciclo_id": ciclo_id,
        "cod_bodega": cod_bodega,
        "lineas_insertadas": len(lineas),
        "estado": "BORRADOR_CONTEO",
        "reemplazo": bool(n_exist and reemplazar),
    }


def listar_ciclos_api(
    *,
    estado: str | None = None,
    cod_bodega: str | None = None,
    limit: int = 15,
) -> list[dict]:
    sb = _sb()
    q = sb.table("conteo_ciclo").select("*").order("created_at", desc=True).limit(limit)
    if estado:
        q = q.eq("estado", estado.strip())
    if cod_bodega:
        q = q.eq("cod_bodega", cod_bodega.strip())
    return q.execute().data or []


def anular_ciclo_api(ciclo_id: str, *, notas: str | None = None) -> dict:
    sb = _sb()
    ciclo_id = (ciclo_id or "").strip()
    upd: dict[str, Any] = {"estado": "ANULADO"}
    if notas:
        upd["notas"] = notas.strip()
    sb.table("conteo_ciclo").update(upd).eq("id", ciclo_id).execute()
    return {"ciclo_id": ciclo_id, "estado": "ANULADO"}


def cmd_listar_ciclos(args: argparse.Namespace) -> None:
    sb = _sb()
    q = sb.table("conteo_ciclo").select("*").order("created_at", desc=True).limit(args.limit)
    if args.estado:
        q = q.eq("estado", args.estado.strip())
    if args.cod_bodega:
        q = q.eq("cod_bodega", args.cod_bodega.strip())
    rows = q.execute().data or []
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


def cmd_ver_envio(args: argparse.Namespace) -> None:
    sb = _sb()
    envio_id = args.envio_id.strip()
    r = sb.table("conteo_envio").select("*").eq("id", envio_id).limit(1).execute()
    envio = (r.data or [None])[0]
    if not envio:
        _err("ENVIO_NOT_FOUND", f"No existe envío id={envio_id}")
    dets = _paginar(sb, "conteo_envio_detalle", "*", [("eq", "envio_id", envio_id)])
    print(json.dumps({"envio": envio, "detalle": dets}, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    p = argparse.ArgumentParser(description="Inventario físico cíclico (conteo)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_crear = sub.add_parser("crear-ciclo", help="Inserta conteo_ciclo (PLANIFICADO)")
    p_crear.add_argument("--anio", type=int, required=True)
    p_crear.add_argument("--semana-iso", type=int, required=True)
    p_crear.add_argument("--cod-bodega", required=True)
    p_crear.add_argument("--spreadsheet-id", default="")
    p_crear.add_argument("--sheet-name", default="CONTEO")
    p_crear.add_argument("--area-etiqueta", default="")
    p_crear.add_argument("--id-humano", default="")
    p_crear.add_argument("--responsable-nombre", default="")
    p_crear.add_argument("--responsable-contacto", default="")
    p_crear.add_argument("--notas", default="")
    p_crear.add_argument("--produccion", action="store_true", help="Sin esto: dry run")
    p_crear.set_defaults(func=cmd_crear_ciclo)

    p_sn = sub.add_parser(
        "snapshot",
        help="Carga conteo_linea desde BD_MP_SISTEMA filtrando cod_bodega del ciclo",
    )
    p_sn.add_argument("--ciclo-id", required=True)
    p_sn.add_argument(
        "--reemplazar",
        action="store_true",
        help="Borra líneas existentes del ciclo y vuelve a cargar",
    )
    p_sn.add_argument("--produccion", action="store_true")
    p_sn.set_defaults(func=cmd_snapshot)

    p_env = sub.add_parser("registrar-envio", help="Valida JSON y persiste conteo_envio + detalle")
    p_env.add_argument("--ciclo-id", required=True)
    p_env.add_argument("--archivo", default="", help="JSON; si omite, lee stdin")
    p_env.add_argument("--idempotency-key", default="")
    p_env.add_argument("--produccion", action="store_true")
    p_env.set_defaults(func=cmd_registrar_envio)

    p_ap = sub.add_parser("aprobar", help="Aprueba líneas PENDIENTE_APROBACION del envío")
    p_ap.add_argument("--envio-id", required=True)
    p_ap.add_argument("--por", required=True, help="Nombre de quien aprueba")
    p_ap.add_argument(
        "--solo-cod-mp",
        default="",
        help="Lista separada por comas; solo esas MPs (resto queda pendiente)",
    )
    p_ap.set_defaults(func=cmd_aprobar)

    p_re = sub.add_parser("rechazar-todo", help="Rechaza todas las líneas pendientes del envío")
    p_re.add_argument("--envio-id", required=True)
    p_re.add_argument("--por", required=True)
    p_re.set_defaults(func=cmd_rechazar_todo)

    p_co = sub.add_parser("contabilizar", help="Inserta mov_inventario para líneas APROBADO")
    p_co.add_argument("--envio-id", required=True)
    p_co.add_argument(
        "--tol",
        type=float,
        default=None,
        help="Override umbral abs(delta): por debajo no se crea movimiento (default 0.001 desde PARAMETROS/BD_CONFIG/env)",
    )
    p_co.add_argument(
        "--registrado-por",
        default="CONTEO_CLI",
        help="Valor mov_inventario.registrado_por",
    )
    p_co.add_argument(
        "--cerrar-ciclo",
        action="store_true",
        help="Pone conteo_ciclo.estado=CONTABILIZADO tras movimientos",
    )
    p_co.add_argument(
        "--recalcular-sheets",
        action="store_true",
        help="Ejecuta recalcular_stock_sheets.py --produccion al final",
    )
    p_co.add_argument("--produccion", action="store_true")
    p_co.set_defaults(func=cmd_contabilizar)

    p_ls = sub.add_parser("listar-ciclos", help="Lista últimos ciclos")
    p_ls.add_argument("--estado", default="")
    p_ls.add_argument("--cod-bodega", default="")
    p_ls.add_argument("--limit", type=int, default=30)
    p_ls.set_defaults(func=cmd_listar_ciclos)

    p_ve = sub.add_parser("ver-envio", help="Muestra envío y detalle")
    p_ve.add_argument("--envio-id", required=True)
    p_ve.set_defaults(func=cmd_ver_envio)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

"""
Control de pagos a proveedores (cuentas por pagar) — pantalla /pagos.

Acceso por usuario + PIN (env PAGOS_USUARIOS="moises:1234,mary:5678").
Sesión firmada con HMAC (env PAGOS_SECRET) enviada en header X-Pagos-Token.
Datos en Supabase: cxp_facturas + cxp_historial (ver sql/add_cxp_facturas.sql).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from supabase import create_client

router = APIRouter()

TABLA = "cxp_facturas"
TABLA_HIST = "cxp_historial"
_HTML = Path(__file__).resolve().parent / "pagos.html"
_SESION_HORAS = 12
_MAX_FALLOS = 5
_BLOQUEO_SEG = 600
_fallos: dict[str, list[float]] = {}
_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
MEDIOS_PAGO = ("TRANSFERENCIA", "EFECTIVO", "CHEQUE", "TARJETA", "DEBITO", "OTRO")
SYNC_CADA_SEG = int(os.getenv("PAGOS_SYNC_CADA_SEG", "3600"))
_sync_lock = threading.Lock()
_ultima_sync: dict = {}


def _sincronizar_cxp(origen: str) -> dict:
    from cuentas_por_pagar import sincronizar

    with _sync_lock:
        desde = (_hoy() - timedelta(days=45)).isoformat()
        try:
            res = sincronizar(desde=desde, dry_run=False)
            _ultima_sync.update(
                {"at": datetime.now().isoformat(), "origen": origen, "nuevas": len(res["nuevos"]),
                 "actualizadas": res["actualizados"], "error": None}
            )
        except Exception as e:
            _ultima_sync.update({"at": datetime.now().isoformat(), "origen": origen, "error": str(e)[:300]})
            raise
        return dict(_ultima_sync)


def _loop_sync() -> None:
    time.sleep(90)
    while True:
        try:
            r = _sincronizar_cxp("auto")
            print(f"[pagos] sync auto: {r.get('nuevas')} nuevas, {r.get('actualizadas')} actualizadas")
        except Exception as e:
            print(f"[pagos] sync auto ERROR: {e}")
        time.sleep(SYNC_CADA_SEG)


if os.getenv("RAILWAY_ENVIRONMENT") and os.getenv("PAGOS_SYNC_AUTO", "1") != "0":
    threading.Thread(target=_loop_sync, name="pagos-sync", daemon=True).start()


def _sb():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _usuarios() -> dict[str, str]:
    out = {}
    for par in (os.getenv("PAGOS_USUARIOS") or "").split(","):
        if ":" in par:
            u, p = par.split(":", 1)
            if u.strip() and p.strip():
                out[u.strip().lower()] = p.strip()
    return out


def _secret() -> bytes:
    s = os.getenv("PAGOS_SECRET") or os.getenv("SUPABASE_KEY") or ""
    return hashlib.sha256(("pagos:" + s).encode()).digest()


def _firmar(usuario: str) -> str:
    exp = int(time.time()) + _SESION_HORAS * 3600
    payload = f"{usuario}|{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def _usuario_sesion(token: str | None) -> str:
    try:
        usuario, exp, sig = base64.urlsafe_b64decode((token or "").encode()).decode().split("|")
    except Exception:
        raise HTTPException(status_code=401, detail="Sesión inválida")
    esperado = hmac.new(_secret(), f"{usuario}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, esperado) or int(exp) < time.time():
        raise HTTPException(status_code=401, detail="Sesión vencida")
    if usuario not in _usuarios():
        raise HTTPException(status_code=401, detail="Usuario sin acceso")
    return usuario


def _json(content) -> JSONResponse:
    return JSONResponse(content=content, headers=_NO_STORE)


def _hoy() -> date:
    import pytz

    return datetime.now(pytz.timezone("America/Guayaquil")).date()


def _estado_visible(r: dict, hoy: date) -> str:
    if r.get("estado_pago") != "PENDIENTE":
        return r.get("estado_pago") or ""
    v = r.get("fecha_vencimiento")
    if v and v < hoy.isoformat():
        return "VENCIDA"
    if v and v <= (hoy + timedelta(days=7)).isoformat():
        return "POR_VENCER"
    return "PENDIENTE"


def _historial(sb, factura_ids: list[str], accion: str, usuario: str, detalle: dict) -> None:
    if not factura_ids:
        return
    sb.table(TABLA_HIST).insert(
        [{"factura_id": i, "accion": accion, "usuario": usuario, "detalle": detalle} for i in factura_ids]
    ).execute()


def _fetch(sb, *, estado: str, desde: str | None, hasta: str | None, ruc: str | None) -> list[dict]:
    rows: list[dict] = []
    off = 0
    while True:
        q = sb.table(TABLA).select("*")
        if estado in ("PENDIENTE", "PAGADA", "ANULADA"):
            q = q.eq("estado_pago", estado)
        elif estado == "VENCIDA":
            q = q.eq("estado_pago", "PENDIENTE").lt("fecha_vencimiento", _hoy().isoformat())
        if estado != "PENDIENTE" and estado != "VENCIDA":
            if desde:
                q = q.gte("fecha_emision", desde)
            if hasta:
                q = q.lte("fecha_emision", hasta)
        if ruc:
            q = q.eq("ruc_proveedor", ruc)
        b = q.order("fecha_vencimiento").order("id").range(off, off + 999).execute().data or []
        rows.extend(b)
        if len(b) < 1000:
            return rows
        off += 1000


# ── Páginas / sesión ─────────────────────────────────────────


@router.get("/pagos", response_class=HTMLResponse)
def pagina_pagos():
    if not _HTML.is_file():
        raise HTTPException(status_code=503, detail="pagos.html no encontrado")
    return HTMLResponse(_HTML.read_text(encoding="utf-8"), headers=_NO_STORE)


@router.post("/api/pagos/login")
async def login(request: Request):
    body = await request.json()
    usuario = str(body.get("usuario") or "").strip().lower()
    pin = str(body.get("pin") or "").strip()
    ahora = time.time()
    recientes = [t for t in _fallos.get(usuario, []) if ahora - t < _BLOQUEO_SEG]
    if len(recientes) >= _MAX_FALLOS:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera 10 minutos.")
    esperado = _usuarios().get(usuario)
    if not esperado or not hmac.compare_digest(pin, esperado):
        _fallos[usuario] = recientes + [ahora]
        raise HTTPException(status_code=401, detail="Usuario o PIN incorrecto")
    _fallos.pop(usuario, None)
    return _json({"token": _firmar(usuario), "usuario": usuario, "medios_pago": MEDIOS_PAGO})


# ── Consultas ────────────────────────────────────────────────


@router.get("/api/pagos/facturas")
def listar_facturas(
    x_pagos_token: str | None = Header(default=None),
    estado: str = Query(default="PENDIENTE"),
    desde: str | None = Query(default=None),
    hasta: str | None = Query(default=None),
    ruc: str | None = Query(default=None),
):
    _usuario_sesion(x_pagos_token)
    sb = _sb()
    hoy = _hoy()
    estado = (estado or "PENDIENTE").upper()
    rows = _fetch(sb, estado=estado, desde=desde, hasta=hasta, ruc=ruc)
    for r in rows:
        r["estado_visible"] = _estado_visible(r, hoy)
        v = r.get("fecha_vencimiento")
        r["dias_para_vencer"] = (date.fromisoformat(v) - hoy).days if v else None

    pend = _fetch(sb, estado="PENDIENTE", desde=None, hasta=None, ruc=None)
    res = {"vencido": 0.0, "por_vencer_7d": 0.0, "pendiente": 0.0, "n_vencidas": 0, "n_por_vencer": 0, "n_pendientes": len(pend)}
    por_prov: dict[str, dict] = {}
    for r in pend:
        t = float(r.get("total") or 0)
        ev = _estado_visible(r, hoy)
        res["pendiente"] += t
        if ev == "VENCIDA":
            res["vencido"] += t
            res["n_vencidas"] += 1
        elif ev == "POR_VENCER":
            res["por_vencer_7d"] += t
            res["n_por_vencer"] += 1
        p = por_prov.setdefault(
            r["ruc_proveedor"],
            {"ruc": r["ruc_proveedor"], "razon_social": r.get("razon_social") or r["ruc_proveedor"], "total": 0.0, "vencido": 0.0, "n": 0},
        )
        p["total"] += t
        p["n"] += 1
        if ev == "VENCIDA":
            p["vencido"] += t
    for k in ("vencido", "por_vencer_7d", "pendiente"):
        res[k] = round(res[k], 2)
    proveedores = sorted(
        ({**p, "total": round(p["total"], 2), "vencido": round(p["vencido"], 2)} for p in por_prov.values()),
        key=lambda p: -p["total"],
    )
    return _json({"hoy": hoy.isoformat(), "facturas": rows, "resumen": res, "proveedores": proveedores,
                  "ultima_sync": _ultima_sync or None})


@router.get("/api/pagos/historial/{factura_id}")
def historial(factura_id: str, x_pagos_token: str | None = Header(default=None)):
    _usuario_sesion(x_pagos_token)
    rows = (
        _sb().table(TABLA_HIST).select("*").eq("factura_id", factura_id)
        .order("creado_at", desc=True).execute().data or []
    )
    return _json({"historial": rows})


# ── Acciones ─────────────────────────────────────────────────


@router.post("/api/pagos/marcar_pagadas")
async def marcar_pagadas(request: Request, x_pagos_token: str | None = Header(default=None)):
    usuario = _usuario_sesion(x_pagos_token)
    body = await request.json()
    ids = [str(i) for i in (body.get("ids") or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="Selecciona al menos una factura")
    fecha_pago = str(body.get("fecha_pago") or "").strip()[:10] or _hoy().isoformat()
    try:
        date.fromisoformat(fecha_pago)
    except ValueError:
        raise HTTPException(status_code=400, detail="Fecha de pago inválida")
    medio = str(body.get("medio_pago") or "").strip().upper()
    if medio not in MEDIOS_PAGO:
        raise HTTPException(status_code=400, detail="Medio de pago inválido")
    sb = _sb()
    actuales = sb.table(TABLA).select("id,estado_pago,total").in_("id", ids).execute().data or []
    pendientes = [r["id"] for r in actuales if r["estado_pago"] == "PENDIENTE"]
    if not pendientes:
        raise HTTPException(status_code=409, detail="Las facturas seleccionadas ya no están pendientes")
    lote = "PAGO-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6].upper()
    datos = {
        "estado_pago": "PAGADA",
        "fecha_pago": fecha_pago,
        "medio_pago": medio,
        "referencia_pago": str(body.get("referencia_pago") or "").strip() or None,
        "comprobante_url": str(body.get("comprobante_url") or "").strip() or None,
        "lote_pago": lote,
        "pagado_por": usuario,
        "pagado_at": datetime.now().isoformat(),
        "actualizado_at": datetime.now().isoformat(),
    }
    notas = str(body.get("notas") or "").strip()
    if notas:
        datos["notas"] = notas
    sb.table(TABLA).update(datos).in_("id", pendientes).eq("estado_pago", "PENDIENTE").execute()
    total = round(sum(float(r.get("total") or 0) for r in actuales if r["id"] in pendientes), 2)
    _historial(sb, pendientes, "PAGADA", usuario, {k: datos[k] for k in ("fecha_pago", "medio_pago", "referencia_pago", "lote_pago")})
    return _json({"ok": True, "marcadas": len(pendientes), "total": total, "lote_pago": lote})


@router.post("/api/pagos/revertir")
async def revertir(request: Request, x_pagos_token: str | None = Header(default=None)):
    usuario = _usuario_sesion(x_pagos_token)
    body = await request.json()
    fid = str(body.get("id") or "")
    motivo = str(body.get("motivo") or "").strip()
    if not fid or not motivo:
        raise HTTPException(status_code=400, detail="Indica la factura y el motivo")
    sb = _sb()
    act = sb.table(TABLA).select("*").eq("id", fid).limit(1).execute().data
    if not act:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    a = act[0]
    if a["estado_pago"] == "PENDIENTE":
        raise HTTPException(status_code=409, detail="La factura ya está pendiente")
    sb.table(TABLA).update(
        {
            "estado_pago": "PENDIENTE",
            "fecha_pago": None,
            "medio_pago": None,
            "referencia_pago": None,
            "comprobante_url": None,
            "lote_pago": None,
            "pagado_por": None,
            "pagado_at": None,
            "actualizado_at": datetime.now().isoformat(),
        }
    ).eq("id", fid).execute()
    _historial(
        sb, [fid], "REVERTIDA", usuario,
        {"motivo": motivo, "estado_anterior": a["estado_pago"], "fecha_pago": a.get("fecha_pago"),
         "referencia_pago": a.get("referencia_pago"), "lote_pago": a.get("lote_pago")},
    )
    return _json({"ok": True})


@router.post("/api/pagos/editar")
async def editar(request: Request, x_pagos_token: str | None = Header(default=None)):
    usuario = _usuario_sesion(x_pagos_token)
    body = await request.json()
    fid = str(body.get("id") or "")
    sb = _sb()
    act = sb.table(TABLA).select("*").eq("id", fid).limit(1).execute().data
    if not act:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    a = act[0]
    cambios: dict = {}
    if body.get("dias_credito") not in (None, ""):
        try:
            dias = int(body["dias_credito"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Días de crédito inválidos")
        if dias < 0 or dias > 365:
            raise HTTPException(status_code=400, detail="Días de crédito fuera de rango")
        if dias != a.get("dias_credito"):
            cambios["dias_credito"] = dias
            cambios["fuente_credito"] = "MANUAL"
            if a.get("fecha_emision"):
                cambios["fecha_vencimiento"] = (
                    date.fromisoformat(a["fecha_emision"]) + timedelta(days=dias)
                ).isoformat()
    if body.get("total") not in (None, ""):
        try:
            total = round(float(str(body["total"]).replace(",", ".")), 2)
        except ValueError:
            raise HTTPException(status_code=400, detail="Total inválido")
        if total < 0:
            raise HTTPException(status_code=400, detail="Total inválido")
        if a.get("total") is None or abs(float(a["total"]) - total) >= 0.005:
            cambios["total"] = total
            cambios["total_estimado"] = False
    if "notas" in body:
        notas = str(body.get("notas") or "").strip() or None
        if notas != a.get("notas"):
            cambios["notas"] = notas
    if body.get("anular"):
        if a["estado_pago"] != "PENDIENTE":
            raise HTTPException(status_code=409, detail="Solo se anulan facturas pendientes")
        cambios["estado_pago"] = "ANULADA"
    if not cambios:
        return _json({"ok": True, "sin_cambios": True})
    sb.table(TABLA).update({**cambios, "actualizado_at": datetime.now().isoformat()}).eq("id", fid).execute()
    _historial(
        sb, [fid], "ANULADA" if body.get("anular") else "EDITADA", usuario,
        {"cambios": cambios, "antes": {k: a.get(k) for k in cambios}},
    )
    return _json({"ok": True, "cambios": cambios})


@router.post("/api/pagos/sincronizar")
def sincronizar_ahora(x_pagos_token: str | None = Header(default=None)):
    usuario = _usuario_sesion(x_pagos_token)
    res = _sincronizar_cxp(usuario)
    return _json({"ok": True, "nuevas": res["nuevas"], "actualizadas": res["actualizadas"], "ultima_sync": res})


@router.get("/api/pagos/export.xlsx")
def exportar(
    token: str = Query(default=""),
    estado: str = Query(default="PENDIENTE"),
    desde: str | None = Query(default=None),
    hasta: str | None = Query(default=None),
    ruc: str | None = Query(default=None),
):
    _usuario_sesion(token)
    from openpyxl import Workbook

    hoy = _hoy()
    rows = _fetch(_sb(), estado=(estado or "PENDIENTE").upper(), desde=desde, hasta=hasta, ruc=ruc)
    wb = Workbook()
    ws = wb.active
    ws.title = "Cuentas por pagar"
    cols = [
        ("Proveedor", "razon_social"), ("RUC", "ruc_proveedor"), ("Factura", "num_factura"),
        ("Emisión", "fecha_emision"), ("Días crédito", "dias_credito"), ("Vence", "fecha_vencimiento"),
        ("Subtotal", "subtotal"), ("IVA", "iva"), ("Total", "total"), ("Estado", "_estado"),
        ("Fecha pago", "fecha_pago"), ("Medio", "medio_pago"), ("Referencia", "referencia_pago"),
        ("Pagado por", "pagado_por"), ("Lote", "lote_pago"), ("Notas", "notas"),
    ]
    ws.append([c[0] for c in cols])
    for r in rows:
        r["_estado"] = _estado_visible(r, hoy)
        ws.append([r.get(k) for _, k in cols])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nombre = f"cuentas_por_pagar_{estado.lower()}_{hoy.isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )

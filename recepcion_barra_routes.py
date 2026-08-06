"""
API: confirmación física de compras SRI de proveedores Tipo=Barra.

POST /api/recepcion_barra/ok
  Header: X-Tatami-Factura-Secret = FACTURA_SHEETS_INGEST_SECRET
  Body: {
    "num_factura": "...",
    "usuario": "felipe@...",
    "dry_run": false,
    "claves_linea": ["num|cod", ...]   // opcional → OK parcial solo esas líneas
  }

GET  /api/recepcion_barra/pendientes
  Lista líneas POR_RECIBIR en staging.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _check_secret(request: Request) -> None:
    secret = request.headers.get("X-Tatami-Factura-Secret")
    expected = (os.getenv("FACTURA_SHEETS_INGEST_SECRET") or "").strip()
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="No autorizado")


@router.get("/ping")
def ping_recepcion(request: Request):
    _check_secret(request)
    from recepcion_compras_barra import barra_requiere_ok

    return {
        "ok": True,
        "sri_barra_requiere_ok": barra_requiere_ok(),
        "nota": "OK total o parcial (claves_linea) → ENTRADA inventario.",
    }


@router.get("/pendientes")
def listar_pendientes(request: Request):
    _check_secret(request)
    from recepcion_compras_barra import listar_por_recibir

    filas = listar_por_recibir(solo_pendientes=True)
    # Agrupar por factura para el menú Sheets
    por_fac: dict[str, dict] = {}
    for f in filas:
        n = (f.get("num_factura") or "").strip()
        if not n:
            continue
        g = por_fac.setdefault(
            n,
            {
                "num_factura": n,
                "razon_social": f.get("razon_social") or "",
                "fecha_factura": f.get("fecha_factura") or "",
                "lineas": 0,
            },
        )
        g["lineas"] += 1
    return {
        "ok": True,
        "total_lineas": len(filas),
        "facturas": list(por_fac.values()),
        "detalle": filas,
    }


@router.post("/ok")
async def confirmar_ok(request: Request):
    _check_secret(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="El cuerpo debe ser un objeto JSON")

    num = str(payload.get("num_factura") or "").strip()
    if not num:
        raise HTTPException(status_code=400, detail="num_factura requerido")

    usuario = str(payload.get("usuario") or "").strip()
    dry_run = bool(payload.get("dry_run") or payload.get("modo_prueba"))
    raw_claves = payload.get("claves_linea") or payload.get("claves") or []
    claves: list[str] = []
    if isinstance(raw_claves, list):
        claves = [str(c).strip() for c in raw_claves if str(c).strip()]

    cantidades: dict[str, float] = {}
    raw_qty = payload.get("cantidades_recibidas") or payload.get("cantidades") or {}
    if isinstance(raw_qty, dict):
        for k, v in raw_qty.items():
            try:
                from numeros_sheets import parse_numero_sheets

                q = parse_numero_sheets(v, default=float("nan"))
                if q != q or q <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            kk = str(k).strip()
            if kk:
                cantidades[kk] = q

    from recepcion_compras_barra import confirmar_factura_ok

    result = confirmar_factura_ok(
        num,
        usuario=usuario,
        dry_run=dry_run,
        claves_linea=claves or None,
        cantidades_recibidas=cantidades or None,
    )
    if not result.get("ok") and not dry_run:
        # Soft fail con 200 + ok:false para que Apps Script muestre el mensaje
        return result
    return result

"""
Traslados masivos entre bodegas desde Google Sheets (Apps Script, botón ACEPTAR).

POST /api/traslado_masivo/enviar
  Header: X-Tatami-Traslado-Secret = TRASLADO_SHEETS_INGEST_SECRET (.env)
  Payload:
    {
      "usuario": "mary@tatami.ec",
      "bodega_origen": "BOD-005",
      "bodega_destino": "BOD-001",
      "modo_prueba": false,
      "lineas": [
        {"producto": "kimchi — SUB-036 — 5200 — gr", "cantidad": 5200, "unidad": "gr"},
        ...
      ]
    }

Política operativa (Sheets):
  - Cualquier par aprobado en bodegas_config.traslado_permitido
  - Stock negativo permitido en origen (no bloquea por stock insuficiente)
  - Solo correos en TRASLADO_SHEETS_EMAILS y/o BD_CONFIG emails_traslado_masivo
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _emails_autorizados() -> set[str]:
    out: set[str] = set()
    raw = (os.getenv("TRASLADO_SHEETS_EMAILS") or "").strip()
    if raw:
        out |= {_norm_email(e) for e in re.split(r"[,;\s\n]+", raw) if e.strip()}
    try:
        from config_sheets import cfg

        v = cfg("emails_traslado_masivo", None)
        if v is not None and str(v).strip():
            out |= {_norm_email(e) for e in re.split(r"[,;\s\n]+", str(v)) if e.strip()}
    except Exception:
        pass
    return out


def _check_traslado_secret(request: Request) -> None:
    secret = request.headers.get("X-Tatami-Traslado-Secret")
    expected = (os.getenv("TRASLADO_SHEETS_INGEST_SECRET") or "").strip()
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="No autorizado")


def _validar_email_usuario(usuario: str) -> None:
    email = _norm_email(usuario)
    if not email or "@" not in email:
        raise HTTPException(status_code=403, detail="Usuario sin correo válido")
    permitidos = _emails_autorizados()
    if not permitidos:
        raise HTTPException(
            status_code=503,
            detail="Falta configurar TRASLADO_SHEETS_EMAILS o emails_traslado_masivo en BD_CONFIG",
        )
    if email not in permitidos:
        raise HTTPException(status_code=403, detail=f"Correo no autorizado: {usuario}")


def _extraer_cod_producto(texto: str) -> str:
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


@router.get("/ping")
def ping_traslado_masivo(request: Request):
    """Diagnóstico: Apps Script verifica URL, secret y pares de bodega."""
    _check_traslado_secret(request)
    from bodegas_config import BODEGAS, traslado_permitido

    pares = []
    for origen, info in sorted(BODEGAS.items()):
        if not info.activa:
            continue
        for destino, info_d in sorted(BODEGAS.items()):
            if not info_d.activa or origen == destino:
                continue
            if traslado_permitido(origen, destino):
                pares.append(f"{origen}→{destino}")
    emails = sorted(_emails_autorizados())
    return {
        "ok": True,
        "pares_traslado": pares,
        "emails_autorizados": len(emails),
        "nota": "Stock negativo permitido; solo correos autorizados.",
    }


@router.post("/enviar")
async def recibir_traslado_masivo(request: Request):
    _check_traslado_secret(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="El cuerpo debe ser un objeto JSON")

    try:
        return _procesar_traslado_masivo(payload)
    except HTTPException:
        raise
    except Exception as e:
        import traceback

        print(f"ERROR traslado_masivo: {traceback.format_exc()}")
        raise HTTPException(
            status_code=503,
            detail=f"Error interno traslado masivo: {type(e).__name__}: {e}",
        ) from e


def _procesar_traslado_masivo(payload: dict):
    from bodegas_config import nombre_bodega, normalizar_cod_bodega, resolver_cod_bodega, traslado_permitido
    from inventario_stock_mp import norm_mp
    from inventario_traslado import costo_ref_desde_filas_maestro, registrar_traslado_mp

    usuario = str(payload.get("usuario") or "").strip()
    _validar_email_usuario(usuario)

    origen = resolver_cod_bodega(payload.get("bodega_origen", ""))
    destino = resolver_cod_bodega(payload.get("bodega_destino", ""))
    if not origen or not destino:
        raise HTTPException(status_code=400, detail="Faltan bodega_origen y/o bodega_destino")
    if origen == destino:
        raise HTTPException(status_code=400, detail="Origen y destino no pueden ser iguales")
    if not traslado_permitido(origen, destino):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Traslado no permitido: {nombre_bodega(origen)} ({origen}) → "
                f"{nombre_bodega(destino)} ({destino})"
            ),
        )

    lineas = payload.get("lineas")
    modo_prueba = bool(payload.get("modo_prueba"))
    if not isinstance(lineas, list) or not lineas:
        raise HTTPException(status_code=400, detail="lineas debe ser un arreglo no vacío")

    from whatsapp_webhook import conectar_supabase, leer_bd_mp_sistema

    rows = leer_bd_mp_sistema(force_refresh=True)
    resultados: list[dict] = []
    ok_count = 0
    err_count = 0
    cods_ok: set[str] = set()

    for idx, ln in enumerate(lineas, start=1):
        producto = str(ln.get("producto") or ln.get("descripcion") or "").strip()
        cod_raw = str(ln.get("cod_mp_sistema") or ln.get("cod_mp") or "").strip()
        cod_buscar = cod_raw or _extraer_cod_producto(producto)
        try:
            cantidad = float(ln.get("cantidad") or 0)
        except (TypeError, ValueError):
            cantidad = 0.0
        unidad_ln = str(ln.get("unidad") or ln.get("unidad_base") or "").strip()

        res: dict = {"producto": producto or cod_buscar, "cantidad": cantidad}
        if cantidad <= 0:
            res["estado"] = "CANTIDAD_INVALIDA"
            err_count += 1
            resultados.append(res)
            continue
        if not cod_buscar:
            res["estado"] = "SIN_CODIGO"
            err_count += 1
            resultados.append(res)
            continue

        from whatsapp_webhook import _resolver_mp_por_nombre

        res_mp = _resolver_mp_por_nombre(
            rows,
            nombre_mp=producto,
            cod_mp=cod_buscar,
            bodega_origen=origen,
        )
        if not res_mp.get("ok"):
            res["estado"] = "NO_RESUELTO"
            res["detalle"] = res_mp.get("error") or res_mp.get("mensaje") or "Sin coincidencia"
            err_count += 1
            resultados.append(res)
            continue

        cod_mp = res_mp["cod_mp"]
        nombre_mp = res_mp.get("nombre_mp") or cod_mp
        res["cod_mp_sistema"] = cod_mp
        res["nombre_mp"] = nombre_mp

        unidad_base = unidad_ln
        stock_origen = None
        costo_ref = 0.0
        for r in rows:
            if norm_mp(r.get("cod_mp_sistema")) != norm_mp(cod_mp):
                continue
            if normalizar_cod_bodega(r.get("cod_bodega", "")) == origen:
                if not unidad_base:
                    unidad_base = str(r.get("unidad_base") or "gr").strip() or "gr"
                try:
                    stock_origen = float(r.get("stock_actual") or 0)
                except (TypeError, ValueError):
                    stock_origen = 0.0
                costo_ref = costo_ref_desde_filas_maestro(rows, cod_mp, origen)
                break
        if not unidad_base:
            for r in rows:
                if norm_mp(r.get("cod_mp_sistema")) == norm_mp(cod_mp):
                    unidad_base = str(r.get("unidad_base") or "gr").strip() or "gr"
                    break
        if not unidad_base:
            unidad_base = "gr" if str(cod_mp).upper().startswith("SUB-") else "uni"
        if costo_ref <= 0:
            costo_ref = costo_ref_desde_filas_maestro(rows, cod_mp, destino)

        res["unidad_base"] = unidad_base
        res["stock_origen_antes"] = stock_origen
        res["stock_origen_despues"] = (
            round((stock_origen or 0) - cantidad, 4) if stock_origen is not None else None
        )

        if modo_prueba:
            res["estado"] = "OK_PRUEBA"
            ok_count += 1
            resultados.append(res)
            continue

        sb = conectar_supabase()
        try:
            mov = registrar_traslado_mp(
                sb,
                cod_mp=cod_mp,
                bodega_origen=origen,
                bodega_destino=destino,
                cantidad=cantidad,
                nombre_mp=nombre_mp,
                unidad_base=unidad_base,
                costo_unitario_ref=costo_ref,
                registrado_por=f"SHEETS:{usuario}",
                recalcular_sheets=False,
                secuencia=idx,
            )
            res["estado"] = "OK"
            res["cod_mov"] = mov.get("cod_mov")
            ok_count += 1
            cods_ok.add(norm_mp(cod_mp))
        except Exception as e:
            res["estado"] = "ERROR"
            res["detalle"] = str(e)
            err_count += 1
        resultados.append(res)

    if not modo_prueba and cods_ok:
        try:
            from recalcular_stock_sheets import recalcular_produccion

            for cod in sorted(cods_ok):
                recalcular_produccion(cod_mp_filtro=cod)
        except Exception as e:
            print(f"WARN traslado_masivo: recalcular Sheets: {e}")

    trx = "TRA-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "ok": err_count == 0,
        "trx": trx if not modo_prueba else "PRUEBA — nada se movió",
        "modo_prueba": modo_prueba,
        "bodega_origen": origen,
        "bodega_destino": destino,
        "usuario": usuario,
        "traslados": ok_count,
        "errores": err_count,
        "lineas": resultados,
    }

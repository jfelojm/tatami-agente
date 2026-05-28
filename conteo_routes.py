"""Rutas HTTP para ingestión de conteo físico desde Google Sheets (Apps Script)."""

import os

from fastapi import APIRouter, HTTPException, Request

from alertas_pipeline import _enviar_wa
from conteo_fisico import ConteoRegistrarError, _sb, registrar_envio_desde_payload
from sesiones_conteo import crear_sesion

router = APIRouter()


def _calcular_deltas(sb, ciclo_id: str, envio_id: str) -> list[dict]:
    """Retorna ítems con |delta_pct| >= 1%. envio_id reservado para futuros filtros por envío."""
    _ = envio_id
    res = (
        sb.table("conteo_linea")
        .select(
            "cod_mp_sistema, nombre_mp, unidad_base, stock_sistema_snapshot, "
            "conteo_fisico, costo_unitario_ref_snapshot"
        )
        .eq("ciclo_id", ciclo_id)
        .execute()
    )
    deltas = []
    for row in res.data or []:
        snap = float(row["stock_sistema_snapshot"] or 0)
        conteo = row.get("conteo_fisico")
        if conteo is None:
            continue
        conteo = float(conteo)
        delta = conteo - snap
        pct = abs(delta / snap * 100) if snap else 0.0
        if pct < 1.0:
            continue
        costo = (
            float(row["costo_unitario_ref_snapshot"])
            if row.get("costo_unitario_ref_snapshot") not in (None, "")
            else None
        )
        valor_delta = delta * costo if costo is not None else None
        deltas.append(
            {
                "cod_mp_sistema": row["cod_mp_sistema"],
                "nombre_mp": row["nombre_mp"],
                "unidad_base": row["unidad_base"],
                "stock_snapshot": snap,
                "conteo_fisico": conteo,
                "delta": delta,
                "delta_pct": round((delta / snap * 100), 2) if snap else 0.0,
                "valor_delta": round(valor_delta, 2) if valor_delta is not None else None,
                "costo_ref": costo,
            }
        )
    return sorted(deltas, key=lambda x: abs(x["delta_pct"]), reverse=True)


def _formatear_informe_wa(ciclo: dict, deltas: list[dict], enviado_por: str) -> str:
    bod = ciclo.get("cod_bodega", "")
    semana = ciclo.get("semana_iso", "")
    anio = ciclo.get("anio", "")
    lineas = [
        f"📊 Informe conteo {bod} — Sem {semana}/{anio}",
        f"👤 Enviado por: {enviado_por}",
        f"⚠️ Diferencias ≥1%: {len(deltas)} ítems",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for d in deltas[:15]:
        signo = "+" if d["delta"] > 0 else ""
        ub = d.get("unidad_base") or ""
        valor_str = f"  ${d['valor_delta']:,.2f}" if d.get("valor_delta") is not None else ""
        lineas.append(
            f"{str(d.get('nombre_mp') or '')[:20]:<20} {signo}{d['delta']:,.1f}{ub}  "
            f"({signo}{d['delta_pct']:.1f}%){valor_str}"
        )
    if len(deltas) > 15:
        lineas.append(f"... y {len(deltas) - 15} ítems más")
    lineas += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "Responde:",
        "• APROBAR TODO",
        "• APROBAR [nombre mp]",
        "• RECHAZAR [nombre mp]",
        "• KARDEX [nombre mp]",
        "• CSV [nombre mp]",
    ]
    return "\n".join(lineas)


@router.post("/enviar")
async def recibir_conteo_sheets(request: Request):
    secret = request.headers.get("X-Tatami-Conteo-Secret")
    expected = (os.getenv("CONTEO_SHEETS_INGEST_SECRET") or "").strip()
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="No autorizado")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="El cuerpo debe ser un objeto JSON")
    ciclo_id = (payload.get("ciclo_id") or "").strip()
    if not ciclo_id:
        raise HTTPException(status_code=400, detail="Falta ciclo_id en el JSON")
    if "lines" not in payload or payload["lines"] is None:
        raise HTTPException(status_code=400, detail="Campos faltantes: ['lines']")
    lines = payload.get("lines")
    if not isinstance(lines, list) or len(lines) == 0:
        raise HTTPException(status_code=400, detail="lines debe ser un arreglo no vacío")
    idem = (payload.get("idempotency_key") or "").strip() or None
    sb = _sb()
    try:
        resultado = registrar_envio_desde_payload(
            sb,
            ciclo_id,
            payload,
            idempotency_key=idem,
            dry_run=False,
        )
    except ConteoRegistrarError as e:
        raise HTTPException(
            status_code=e.http_status,
            detail={"code": e.code, "message": e.message, "details": e.details},
        ) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # --- Informe WA a Moisés ---
    if not resultado.get("idempotent_hit"):
        try:
            envio_id = resultado.get("envio_id")
            enviado_por = (payload.get("enviado_por") or "").strip() or "Desconocido"
            ciclo_res = sb.table("conteo_ciclo").select("*").eq("id", ciclo_id).execute()
            ciclo = ciclo_res.data[0] if ciclo_res.data else {}
            deltas = _calcular_deltas(sb, ciclo_id, str(envio_id or ""))
            print(f"[conteo_routes] deltas calculados: {len(deltas)}")
            numero_moises = (os.getenv("ALERTA_WA_MOISES") or "").strip()
            numero_felipe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
            print(f"[conteo_routes] numero_moises: '{numero_moises}'")
            if deltas and numero_moises and envio_id:
                informe = _formatear_informe_wa(ciclo, deltas, enviado_por)
                print(f"[conteo_routes] enviando WA...")
                _enviar_wa(numero_moises, informe)
                if numero_felipe:
                    _enviar_wa(numero_felipe, informe)
                crear_sesion(numero_moises, str(envio_id), ciclo_id, deltas)
                if numero_felipe:
                    crear_sesion(numero_felipe, str(envio_id), ciclo_id, deltas)
                print(f"[conteo_routes] sesion creada")
            elif numero_moises:
                _enviar_wa(numero_moises, "Conteo recibido sin diferencias >= 1%.")
        except Exception as e:
            print(f"[conteo_routes] Error enviando informe WA: {e}")

    return resultado

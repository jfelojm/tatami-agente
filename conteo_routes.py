"""Rutas HTTP para ingestión de conteo físico desde Google Sheets (Apps Script)."""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException, Request

from alertas_pipeline import _enviar_wa
from conteo_fisico import ConteoRegistrarError, _sb, registrar_envio_desde_payload
from sesiones_conteo import crear_sesion

router = APIRouter()

# WhatsApp Cloud API ~4096; dejar margen para encabezado de parte.
_WA_CONTEO_MAX = 3500


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
        pct_abs = abs(delta / snap * 100) if snap else 0.0
        if pct_abs < 1.0:
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


def _resumen_saldo_deltas(deltas: list[dict]) -> dict:
    """Saldo monetario: positivos, negativos y neto (pos − |neg|)."""
    pos = neg = 0.0
    n_pos = n_neg = 0
    sin_costo = 0
    for d in deltas:
        v = d.get("valor_delta")
        if v is None:
            sin_costo += 1
            continue
        if v >= 0:
            pos += float(v)
            n_pos += 1
        else:
            neg += float(v)  # negativo
            n_neg += 1
    return {
        "valor_positivo": round(pos, 2),
        "valor_negativo": round(neg, 2),
        "saldo_neto": round(pos + neg, 2),  # pos − |neg|
        "n_pos": n_pos,
        "n_neg": n_neg,
        "sin_costo": sin_costo,
    }


def _linea_delta_wa(d: dict) -> str:
    delta = float(d["delta"])
    pct = float(d["delta_pct"])
    ub = d.get("unidad_base") or ""
    signo_d = "+" if delta > 0 else ""
    signo_p = "+" if pct > 0 else ""
    valor_str = (
        f"  ${d['valor_delta']:,.2f}" if d.get("valor_delta") is not None else ""
    )
    return (
        f"{str(d.get('nombre_mp') or '')[:22]:<22} {signo_d}{delta:,.1f}{ub} "
        f"({signo_p}{pct:.1f}%){valor_str}"
    )


def _formatear_informe_wa_partes(
    ciclo: dict, deltas: list[dict], enviado_por: str
) -> list[str]:
    """Informe completo (todos los ítems) partido en mensajes ≤ _WA_CONTEO_MAX."""
    bod = ciclo.get("cod_bodega", "")
    semana = ciclo.get("semana_iso", "")
    anio = ciclo.get("anio", "")
    saldo = _resumen_saldo_deltas(deltas)
    cab = [
        f"📊 Informe conteo {bod} — Sem {semana}/{anio}",
        f"👤 Enviado por: {enviado_por}",
        f"⚠️ Diferencias ≥1%: {len(deltas)} ítems",
        (
            f"💰 Saldo $: +{saldo['valor_positivo']:,.2f} "
            f"({saldo['n_pos']}) / {saldo['valor_negativo']:,.2f} ({saldo['n_neg']}) "
            f"→ neto ${saldo['saldo_neto']:,.2f}"
        ),
    ]
    if saldo["sin_costo"]:
        cab.append(f"(sin costo ref: {saldo['sin_costo']} ítems)")
    cab.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    pie = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "Responde:",
        "• APROBAR TODO",
        "• APROBAR [nombre mp]",
        "• RECHAZAR [nombre mp]",
        "• KARDEX [nombre mp]",
        "• CSV [nombre mp]",
    ]

    if not deltas:
        return ["\n".join(cab + ["(sin diferencias ≥1%)"] + pie)]

    lineas_items = [_linea_delta_wa(d) for d in deltas]
    partes: list[str] = []
    i = 0
    total = len(lineas_items)
    while i < total:
        es_primera = not partes
        bloque = list(cab) if es_primera else [
            f"📊 Conteo {bod} (cont. {len(partes) + 1})",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        while i < total:
            cand = "\n".join(bloque + [lineas_items[i]] + (pie if i == total - 1 else []))
            if len(cand) > _WA_CONTEO_MAX and len(bloque) > (len(cab) if es_primera else 2):
                break
            bloque.append(lineas_items[i])
            i += 1
        if i >= total:
            bloque.extend(pie)
        else:
            bloque.append(f"… sigue ({total - i} ítems)")
        partes.append("\n".join(bloque))
        es_primera = False
    # numerar partes si hay más de una
    if len(partes) > 1:
        n = len(partes)
        out = []
        for idx, p in enumerate(partes, 1):
            out.append(f"[{idx}/{n}]\n{p}")
        return out
    return partes


def _formatear_informe_wa(ciclo: dict, deltas: list[dict], enviado_por: str) -> str:
    """Compat: un solo string (puede superar límite WA; preferir _formatear_informe_wa_partes)."""
    return "\n\n".join(_formatear_informe_wa_partes(ciclo, deltas, enviado_por))


def _enviar_informe_conteo_wa(numero: str, partes: list[str]) -> None:
    for i, parte in enumerate(partes):
        _enviar_wa(numero, parte)
        if i < len(partes) - 1:
            time.sleep(1.1)


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
                partes = _formatear_informe_wa_partes(ciclo, deltas, enviado_por)
                print(
                    f"[conteo_routes] enviando WA... partes={len(partes)} "
                    f"chars={[len(p) for p in partes]}"
                )
                _enviar_informe_conteo_wa(numero_moises, partes)
                if numero_felipe:
                    _enviar_informe_conteo_wa(numero_felipe, partes)
                crear_sesion(numero_moises, str(envio_id), ciclo_id, deltas)
                if numero_felipe:
                    crear_sesion(numero_felipe, str(envio_id), ciclo_id, deltas)
                print(f"[conteo_routes] sesion creada")
            elif numero_moises:
                _enviar_wa(numero_moises, "Conteo recibido sin diferencias >= 1%.")
        except Exception as e:
            print(f"[conteo_routes] Error enviando informe WA: {e}")

    return resultado

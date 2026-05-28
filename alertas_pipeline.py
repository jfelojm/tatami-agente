"""
Alertas de alto nivel del pipeline (reconciliación, pasos omitidos).

Usa alertas_tatami (log, webhook, WhatsApp si está configurado en .env).
"""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

from dotenv import load_dotenv

load_dotenv(override=True)

# Límite práctico WhatsApp (texto); Felipe lleva lista larga de sin_match.
_WA_MAX_BODY = 4000


def enviar_mensaje_wa(numero_raw: str, texto: str, *, etiqueta: str = "alerta") -> bool:
    """
    Envío WhatsApp saliente (mismo canal que alerta_fallo).
    Retorna True si se envió o se omitió por config benigna; False si hubo fallo real.
    """
    from alertas_tatami import enviar_whatsapp_texto, log_envio_wa

    ok, msg = enviar_whatsapp_texto(numero_raw, texto)
    log_envio_wa(etiqueta, numero_raw, ok, msg)
    return ok


def _enviar_wa(numero_raw: str, texto: str) -> bool:
    """Alias corto para rutas HTTP (conteo, etc.); mismo comportamiento que enviar_mensaje_wa."""
    return enviar_mensaje_wa(numero_raw, texto)


def _sin_match_lineas_resumen(raw: list) -> list[str]:
    """Ítems sin match que deben verse en WA (excluye IGNORADO y REGISTRADO en pendientes)."""
    out: list[str] = []
    for it in raw or []:
        if isinstance(it, dict):
            est = (it.get("estado") or "").strip().upper()
            if est in ("IGNORADO", "REGISTRADO"):
                continue
            d = (it.get("descripcion") or "").strip()
            if d:
                out.append(d)
        else:
            s = str(it).strip()
            if s:
                out.append(s)
    return out


def enviar_resumen_facturas(
    resumen: dict[str, Any],
    *,
    pipeline_error: str | None = None,
) -> None:
    """
    Notifica cierre del módulo de facturas (pipeline diario).
    Moisés: compacto. Felipe: mismo bloque + lista sin_match + error de pipeline si hubo.
    """
    mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
    if not mo and not fe:
        return

    completas = int(resumen.get("completas") or 0)
    parciales = int(resumen.get("parciales") or 0)
    total_usd = float(resumen.get("total_usd") or 0.0)
    sin_xmls = bool(resumen.get("sin_xmls"))
    sin_match = _sin_match_lineas_resumen(list(resumen.get("sin_match") or []))
    n_sin = len(sin_match)
    xmls_carpeta = int(resumen.get("xmls_en_carpeta") or 0)
    omitidos = int(resumen.get("xmls_omitidos_completa") or 0)
    proc = int(resumen.get("total_procesadas") or 0)

    lineas_base = [
        "📦 *Facturas procesadas*",
        f"Completas: {completas} | Parciales: {parciales}",
        f"Total ingresado: ${total_usd:.2f}",
    ]
    if sin_xmls:
        lineas_base.append("ℹ️ No había XMLs nuevos en Drive hoy.")
    elif xmls_carpeta > 0 and proc == 0:
        lineas_base.append(
            f"ℹ️ Había {xmls_carpeta} XML en carpeta; 0 aplicados esta corrida "
            f"({omitidos} omitidos ya COMPLETA). Revisar log del pipeline si esperabas movimientos nuevos."
        )

    lineas_mois = list(lineas_base)
    if pipeline_error:
        lineas_mois.append("⚠️ Error al ejecutar el módulo de facturas (revisar log).")
    elif n_sin > 0:
        lineas_mois.append(f"⚠️ Ítems sin match ({n_sin}) — revisar catálogo / pendientes.")

    texto_mois = "\n".join(lineas_mois).strip()
    if len(texto_mois) > _WA_MAX_BODY:
        texto_mois = texto_mois[: _WA_MAX_BODY - 20] + "\n…(truncado)"

    lineas_fe = list(lineas_base)
    if pipeline_error:
        lineas_fe.append(f"🛠 *Error pipeline:*\n{pipeline_error}")
    if n_sin > 0:
        lineas_fe.append(f"⚠️ Ítems sin match ({n_sin}):")
        for item in sin_match:
            lineas_fe.append(f"- {item}")
    texto_fe = "\n".join(lineas_fe).strip()
    if len(texto_fe) > _WA_MAX_BODY:
        texto_fe = texto_fe[: _WA_MAX_BODY - 40] + "\n…(mensaje truncado, ver log)"

    if mo:
        enviar_mensaje_wa(mo, texto_mois, etiqueta="Moisés facturas")
    if fe:
        enviar_mensaje_wa(fe, texto_fe, etiqueta="Felipe facturas")
    if not mo and not fe:
        print("  WA [OMITIDO] facturas: ALERTA_WA_FELIPE y ALERTA_WA_MOISES vacíos")


def alerta_ventas_sin_receta(items: list, fecha: str) -> None:
    """
    Platos vendidos sin filas aplicables en BD_RECETAS_DETALLE (no hubo descargo).
    Agrupa por cod_smart_menu + variedad. Best-effort: errores WA solo se loguean.
    """
    from collections import defaultdict

    mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
    if not items or (not mo and not fe):
        return

    fecha_l = (fecha or "").strip() or "?"

    agg: dict[tuple[str, str], dict[str, Any]] = {}
    counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    for it in items:
        if not isinstance(it, dict):
            continue
        cod = str(it.get("cod_smart_menu") or "").strip()
        var = str(it.get("variedad") or "").strip()
        k = (cod, var)
        counts[k] += 1
        if k not in agg:
            agg[k] = {
                "cod_smart_menu": cod,
                "variedad": var,
                "nombre": str(it.get("nombre") or "").strip() or "(sin nombre)",
            }

    filas = sorted(
        agg.items(),
        key=lambda kv: (counts[kv[0]] * -1, kv[1]["nombre"].lower()),
    )

    intro = (
        "Los siguientes platos se vendieron pero no tienen receta "
        "en BD_RECETAS_DETALLE. No se descontó inventario."
    )
    accion = (
        "Acción: agregar receta en BD_RECETAS_DETALLE o mapear "
        "cod_smart_menu en BD_PRODUCTOS."
    )

    lineas_mois = [
        f"⚠️ *Ventas sin receta — {fecha_l}*",
        intro,
        "",
    ]
    lineas_fe = [
        f"⚠️ *Ventas sin receta — {fecha_l}*",
        intro,
        "",
    ]
    for _k, info in filas:
        n = counts[_k]
        nom = info["nombre"]
        cod_ex = info["cod_smart_menu"]
        var_ex = info["variedad"]
        lineas_mois.append(f"- {nom} — {n} veces")
        lineas_fe.append(f"- {nom} ({cod_ex} / {var_ex}) — {n} veces")

    lineas_mois.extend(["", accion])
    lineas_fe.extend(["", accion])

    texto_mois = "\n".join(lineas_mois).strip()
    texto_fe = "\n".join(lineas_fe).strip()
    if len(texto_mois) > _WA_MAX_BODY:
        texto_mois = texto_mois[: _WA_MAX_BODY - 24] + "\n…(truncado, ver Felipe/log)"
    if len(texto_fe) > _WA_MAX_BODY:
        texto_fe = texto_fe[: _WA_MAX_BODY - 40] + "\n…(truncado, ver log)"

    try:
        if mo:
            enviar_mensaje_wa(mo, texto_mois)
    except Exception as e:
        print(f"  WARN: alerta_ventas_sin_receta WA Moisés: {e}")
    try:
        if fe:
            enviar_mensaje_wa(fe, texto_fe)
    except Exception as e:
        print(f"  WARN: alerta_ventas_sin_receta WA Felipe: {e}")


def alerta_fallo(
    *,
    modulo: str,
    razon: str,
    pasos_ejecutados: Sequence[str] | None = None,
    pasos_pendientes: Sequence[str] | None = None,
    fecha: str | None = None,
) -> None:
    """
    Registra fallo y envía WhatsApp (Moisés corto, Felipe con pasos pendientes).
    """
    from alertas_tatami import enviar_alerta, enviar_whatsapp_texto

    pasos_ejecutados = list(pasos_ejecutados or [])
    pasos_pendientes = list(pasos_pendientes or [])
    fecha = (fecha or "").strip() or "?"

    detalle = {
        "modulo": modulo,
        "razon": razon,
        "fecha": fecha,
        "pasos_ejecutados": pasos_ejecutados,
        "pasos_pendientes": pasos_pendientes,
    }
    enviar_alerta(
        f"Pipeline fallo: {modulo}",
        json.dumps(detalle, ensure_ascii=False, indent=2),
        estado="ERROR",
    )

    mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()

    linea_mois = f"⚠ {modulo} | {razon} | {fecha}"
    pend_txt = ", ".join(pasos_pendientes) if pasos_pendientes else "(ninguno)"
    linea_felipe = f"{linea_mois}\n\n✗ No ejecutados: {pend_txt}"

    if mo:
        ok, msg = enviar_whatsapp_texto(mo, linea_mois)
        if not ok:
            print(f"  WARN: WA Moisés: {msg}")
    if fe:
        ok, msg = enviar_whatsapp_texto(fe, linea_felipe)
        if not ok:
            print(f"  WARN: WA Felipe: {msg}")


def enviar_resumen_corrida_horario(
    fecha: str,
    *,
    resumen_facturas: dict | None = None,
    skip_reconciliar: bool = False,
    notas: list[str] | None = None,
) -> None:
    """
    Resumen único al cerrar pipeline horario (Felipe + Moisés).
    Complementa el bloque de facturas; no sustituye alertas de error.
    """
    from alertas_tatami import resumen_config_wa

    rf = resumen_facturas or {}
    proc = int(rf.get("total_procesadas") or 0)
    compl = int(rf.get("completas") or 0)
    parc = int(rf.get("parciales") or 0)
    usd = float(rf.get("total_usd") or 0.0)
    n_sin = len(_sin_match_lineas_resumen(list(rf.get("sin_match") or [])))

    lineas = [
        f"📋 *Corrida horaria Tatami* — {fecha}",
        f"Estado: pipeline completado",
        f"Facturas: {proc} proc. | {compl} compl. | {parc} parc. | ${usd:.2f}",
    ]
    if n_sin:
        lineas.append(f"⚠ {n_sin} ítems sin match (detalle en mensaje de facturas si aplica)")
    if skip_reconciliar:
        lineas.append("ℹ Reconciliación ventas omitida (--skip-reconciliar)")
    for n in notas or []:
        if (n or "").strip():
            lineas.append(f"• {(n or '').strip()}")
    lineas.append(f"\n{resumen_config_wa()}")
    texto = "\n".join(lineas).strip()[:_WA_MAX_BODY]

    mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
    if not mo and not fe:
        print("  WA [OMITIDO] resumen horario: sin ALERTA_WA_* en .env")
        return
    if mo:
        enviar_mensaje_wa(mo, texto, etiqueta="Moisés resumen horario")
    if fe:
        enviar_mensaje_wa(fe, texto, etiqueta="Felipe resumen horario")


def alerta_ok(*, fecha: str | None = None, detalle: str | None = None) -> None:
    """Felipe + Moisés — confirmación pipeline."""
    from datetime import date

    from alertas_tatami import alerta_wa_pipeline_ok

    f = (fecha or "").strip() or date.today().isoformat()
    alerta_wa_pipeline_ok(f, detalle=detalle)

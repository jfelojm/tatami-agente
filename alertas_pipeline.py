"""
Alertas de alto nivel del pipeline (reconciliación, pasos omitidos).

Usa alertas_tatami (log, webhook, WhatsApp si está configurado en .env).
"""

from __future__ import annotations

import json
import os
from typing import Sequence

from dotenv import load_dotenv

load_dotenv(override=True)


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


def alerta_ok(*, fecha: str | None = None) -> None:
    """Solo Felipe — mismo criterio que pipeline_diario."""
    from datetime import date

    from alertas_tatami import alerta_wa_pipeline_ok

    f = (fecha or "").strip() or date.today().isoformat()
    alerta_wa_pipeline_ok(f)

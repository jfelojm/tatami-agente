"""
Alertas para fallos del pipeline / reconciliación.

Variables de entorno (opcionales):
  TATAMI_ALERT_WEBHOOK_URL  URL HTTP(S) que recibe POST JSON: {"text": "...", "titulo": "...", "detalle": "..."}
  TATAMI_ALERT_LOG_PATH     Añade una línea al archivo (UTF-8).

WhatsApp Cloud API (mismas credenciales que whatsapp_webhook / Meta):
  WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN (obligatorias para enviar WA)
  ALERTA_WA_FELIPE, ALERTA_WA_MOISES  Números con código país, solo dígitos (ej. 5939...)
  TATAMI_WA_SKIP=1         No envía WA (pruebas); log/webhook siguen activos

Nota Meta: fuera de la ventana de 24h con el usuario, puede exigir plantilla aprobada;
si el envío falla, ver el error en consola y el panel de WhatsApp en developers.facebook.com.

No lanza excepción si webhook/WA fallan; imprime WARN en consola.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

WA_GRAPH_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0").strip() or "v21.0"


def enviar_alerta(
    titulo: str,
    detalle: str,
    *,
    estado: str = "ERROR",
) -> None:
    """
    titulo: una línea corta (ej. fallo reconciliación).
    detalle: cuerpo multilínea (contexto, números).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bloque = f"[{estado}] {titulo}\n{detalle}\n({ts})"
    print(f"\n*** ALERTA ***\n{bloque}\n")

    log_path = (os.getenv("TATAMI_ALERT_LOG_PATH") or "").strip()
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(bloque + "\n---\n")
        except OSError as e:
            print(f"  WARN: no se pudo escribir TATAMI_ALERT_LOG_PATH: {e}")

    url = (os.getenv("TATAMI_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return
    if not requests:
        print("  WARN: requests no disponible; webhook omitido")
        return

    payload = {
        "text": f"*{titulo}*\n```\n{detalle[:3500]}\n```",
        "titulo": titulo,
        "detalle": detalle,
        "estado": estado,
        "ts": ts,
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"  WARN: webhook HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  WARN: webhook fallo: {e}")


def _wa_disabled() -> bool:
    return (os.getenv("TATAMI_WA_SKIP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "si",
        "sí",
    )


def _solo_digitos(num_raw: str) -> str:
    return "".join(c for c in (num_raw or "") if c.isdigit())


def enviar_whatsapp_texto(numero_raw: str, cuerpo: str) -> tuple[bool, str]:
    """
    Envía un mensaje de texto por WhatsApp Cloud API.
    numero_raw: ej. 593987654321 o +593...
    """
    if _wa_disabled():
        return False, "TATAMI_WA_SKIP activo"
    if not requests:
        return False, "requests no instalado"
    to = _solo_digitos(numero_raw)
    if not to:
        return False, "numero vacio"
    body = (cuerpo or "").strip()[:4096]
    if not body:
        return False, "cuerpo vacio"

    pid = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not pid or not token:
        return False, "falta WHATSAPP_PHONE_NUMBER_ID o WHATSAPP_ACCESS_TOKEN en .env"

    url = f"https://graph.facebook.com/{WA_GRAPH_VERSION}/{pid}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        data = {}
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 400:
            err = data.get("error", {})
            msg = err.get("message", r.text[:300])
            return False, f"HTTP {r.status_code}: {msg}"
        return True, "ok"
    except Exception as e:
        return False, str(e)


def alerta_wa_reconciliacion_fallo(rep: dict) -> None:
    """
    Moisés: mensaje corto. Felipe: mismo bloque + pasos del pipeline no ejecutados.
    """
    fecha = rep.get("fecha", "?")
    tol = float(rep.get("tol_abs") or 0)
    diff = float(rep.get("diff_sub") or 0)
    grid = float(rep.get("grid_subtotal_sin_iva") or 0)
    hist = float(rep.get("hist_subtotal") or 0)

    linea_mois = (
        f"⚠ Reconciliación ventas — diff ${diff:.2f} (tol ${tol:.2f}) | "
        f"grid ${grid:.2f} vs hist ${hist:.2f} | fecha {fecha}"
    )
    linea_felipe = (
        linea_mois
        + "\n\n✗ No ejecutados: Descargo, Facturas, Recalcular stock, PAR"
    )

    mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()

    if mo:
        ok, msg = enviar_whatsapp_texto(mo, linea_mois)
        if not ok:
            print(f"  WARN: WA Moisés: {msg}")
    if fe:
        ok, msg = enviar_whatsapp_texto(fe, linea_felipe)
        if not ok:
            print(f"  WARN: WA Felipe: {msg}")


def alerta_wa_pipeline_ok(fecha: str) -> None:
    """Solo Felipe — confirmación diaria sin saturar a Moisés."""
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
    if not fe:
        return
    txt = f"✅ Pipeline OK — {fecha}"
    ok, msg = enviar_whatsapp_texto(fe, txt)
    if not ok:
        print(f"  WARN: WA Felipe (OK): {msg}")


def alerta_wa_ventas_strict_fallo(fecha: str, codigo: int) -> None:
    """Fallo de ventas con --strict: avisa a ambos."""
    txt = (
        f"⚠ ventas_smartmenu falló (--strict) código {codigo} | fecha {fecha}. "
        "Revisar PC / red / Smart Menu."
    )
    for env_k in ("ALERTA_WA_MOISES", "ALERTA_WA_FELIPE"):
        n = (os.getenv(env_k) or "").strip()
        if n:
            ok, msg = enviar_whatsapp_texto(n, txt)
            if not ok:
                print(f"  WARN: WA {env_k}: {msg}")

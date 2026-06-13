"""
Alertas para fallos del pipeline / reconciliación.

Variables de entorno (opcionales):
  TATAMI_ALERT_WEBHOOK_URL  URL HTTP(S) que recibe POST JSON: {"text": "...", "titulo": "...", "detalle": "..."}
  TATAMI_ALERT_LOG_PATH     Añade una línea al archivo (UTF-8).

WhatsApp Cloud API (mismas credenciales que whatsapp_webhook / Meta):
  WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN (obligatorias para enviar WA)
  ALERTA_WA_FELIPE, ALERTA_WA_MOISES  Números con código país, solo dígitos (ej. 5939...)
  ALERTA_WA_JACKY, ALERTA_WA_EDUARDO  Confirmación bodega ingreso (001/005 vs 002/003)
  TATAMI_WA_SKIP=1         No envía WA (pruebas); log/webhook siguen activos
  TATAMI_ALERT_STOCK_NEGATIVO=1  Reactiva alertas WA/webhook por stock < 0 tras descargo
                                 (por defecto suspendidas hasta nuevo aviso)

Nota Meta: fuera de la ventana de 24h con el usuario, puede exigir plantilla aprobada;
si el envío falla, ver el error en consola y el panel de WhatsApp en developers.facebook.com.

No lanza excepción si webhook/WA fallan; imprime WARN en consola.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.is_file():
        load_dotenv(_env_path, override=True)
except ImportError:
    pass

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


def _ultimos_digitos(numero_raw: str, n: int = 4) -> str:
    d = _solo_digitos(numero_raw)
    return d[-n:] if len(d) >= n else (d or "????")


def log_envio_wa(etiqueta: str, numero_raw: str, ok: bool, detalle: str) -> None:
    estado = "ENVIADO" if ok else "FALLO"
    print(f"  WA [{estado}] {etiqueta} destino …{_ultimos_digitos(numero_raw)}: {detalle}")


def resumen_config_wa() -> str:
    if _wa_disabled():
        return "WA desactivado (TATAMI_WA_SKIP=1)"
    pid = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    tok = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
    mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
    partes = []
    if not pid or not tok:
        partes.append("API Meta: INCOMPLETA")
    else:
        partes.append(f"API Meta: OK (phone_id …{pid[-6:] if len(pid) > 6 else pid})")
    partes.append(
        f"Felipe …{_ultimos_digitos(fe)}" if fe else "Felipe: NO configurado"
    )
    partes.append(
        f"Moisés …{_ultimos_digitos(mo)}" if mo else "Moisés: NO configurado"
    )
    return " | ".join(partes)


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
        mid = ""
        try:
            msgs = (data.get("messages") or [])
            if msgs and isinstance(msgs[0], dict):
                mid = str(msgs[0].get("id") or "")[:24]
        except (TypeError, KeyError, IndexError):
            pass
        return True, f"ok id={mid}" if mid else "ok"
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
        log_envio_wa("Moisés", mo, ok, msg)
    if fe:
        ok, msg = enviar_whatsapp_texto(fe, linea_felipe)
        log_envio_wa("Felipe", fe, ok, msg)


def alerta_wa_smart_menu_no_disponible(rep: dict) -> None:
    """Smart Menu apagado/fuera de red: cuadre omitido, pipeline sigue."""
    fecha = rep.get("fecha", "?")
    motivo = rep.get("smart_menu_motivo") or "no accesible"
    hist_lineas = int(rep.get("hist_lineas_tabla") or 0)
    hist_neto = float(rep.get("hist_ventas_netas") or 0)

    linea_mois = (
        f"⚠ Smart Menu no accesible — reconciliación omitida | fecha {fecha} "
        f"({motivo})"
    )
    extra = ""
    if hist_lineas:
        extra = (
            f"\n\nhist_ventas: {hist_lineas} líneas, ${hist_neto:.2f} neto. "
            "Pipeline continúa; cuadre pendiente cuando el servidor responda."
        )
    linea_felipe = linea_mois + extra

    mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
    fe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()

    if mo:
        ok, msg = enviar_whatsapp_texto(mo, linea_mois)
        log_envio_wa("Moisés", mo, ok, msg)
    if fe:
        ok, msg = enviar_whatsapp_texto(fe, linea_felipe)
        log_envio_wa("Felipe", fe, ok, msg)


def alerta_wa_pipeline_ok(fecha: str, *, detalle: str | None = None) -> None:
    """Confirmación de corrida: Felipe y Moisés (mismo cuerpo; Felipe puede llevar más en otros avisos)."""
    cuerpo = f"✅ Pipeline Tatami OK — {fecha}"
    if detalle:
        cuerpo = f"{cuerpo}\n{detalle.strip()}"
    cuerpo += (
        "\n\n(Llega desde la línea WhatsApp Business del local en Meta; "
        "no desde un chat personal.)"
    )
    for env_k, etiqueta in (
        ("ALERTA_WA_FELIPE", "Felipe"),
        ("ALERTA_WA_MOISES", "Moisés"),
    ):
        n = (os.getenv(env_k) or "").strip()
        if not n:
            print(f"  WA [OMITIDO] {etiqueta}: {env_k} vacío en .env")
            continue
        ok, msg = enviar_whatsapp_texto(n, cuerpo)
        log_envio_wa(etiqueta, n, ok, msg)


def alerta_stock_negativo_habilitada() -> bool:
    """
    Alertas de stock negativo tras descargo: suspendidas por defecto.
    Para reactivar: TATAMI_ALERT_STOCK_NEGATIVO=1 (o true/si) en .env
    """
    v = (os.getenv("TATAMI_ALERT_STOCK_NEGATIVO") or "").strip().lower()
    return v in ("1", "true", "yes", "si", "sí")


def _enviar_a_lista(numeros: list[str], cuerpo: str, *, etiqueta: str = "lista") -> None:
    for n in numeros:
        if not n:
            continue
        ok, msg = enviar_whatsapp_texto(n, cuerpo)
        log_envio_wa(etiqueta, n, ok, msg)


def alerta_wa_descargo_stock_negativo(items: list[dict]) -> None:
    """
    items: [{cod_mp, nombre_mp, cod_bodega, stock_restante, unidad, cod_venta}, ...]
    """
    if not items:
        return
    if not alerta_stock_negativo_habilitada():
        print(
            f"  INFO: {len(items)} línea(s) con stock negativo tras descargo — "
            "alertas suspendidas (TATAMI_ALERT_STOCK_NEGATIVO=1 para reactivar)."
        )
        return
    lineas = []
    for it in items[:25]:
        lineas.append(
            f"• {it.get('cod_mp')} {it.get('nombre_mp', '')[:40]} @ {it.get('cod_bodega')}: "
            f"stock {it.get('stock_restante')} {it.get('unidad', '')} "
            f"(venta {it.get('cod_venta', '')})"
        )
    extra = f"\n… y {len(items) - 25} más" if len(items) > 25 else ""
    cuerpo = (
        "⚠ Descargo inventario — stock negativo en cocina/barra\n"
        + "\n".join(lineas)
        + extra
    )
    enviar_alerta("Descargo stock negativo", cuerpo, estado="WARN")
    nums = []
    for env_k in ("ALERTA_WA_MOISES", "ALERTA_WA_FELIPE"):
        n = (os.getenv(env_k) or "").strip()
        if n:
            nums.append(n)
    _enviar_a_lista(nums, cuerpo)


def alerta_wa_factura_bodega_pendiente(
    factura: dict,
    pendientes: list[dict],
) -> None:
    """
    pendientes: [{descripcion, cod_mp, default, solicitada, clave}, ...]
    Notifica a Jacky y/o Eduardo según bodegas involucradas.
    """
    if not pendientes:
        return
    from bodegas_config import numeros_confirmacion_bodega, nombre_bodega

    num_f = (factura.get("num_factura") or "").strip()
    prov = (factura.get("razon_social") or "").strip()
    lineas = []
    destinatarios: set[str] = set()
    for p in pendientes[:20]:
        sol = p.get("solicitada") or p.get("default")
        lineas.append(
            f"• {p.get('descripcion', '')[:50]}\n"
            f"  default {nombre_bodega(p.get('default'))} → ingreso {nombre_bodega(sol)}"
        )
        for b in (p.get("default"), p.get("solicitada")):
            for n in numeros_confirmacion_bodega(b):
                destinatarios.add(n)
    cuerpo = (
        f"📦 Factura pendiente confirmación bodega\n"
        f"Proveedor: {prov}\nFactura: {num_f}\n\n"
        + "\n".join(lineas)
        + "\n\nConfirme bodega de ingreso y reprocese con bodegas_confirmadas."
    )
    enviar_alerta("Factura bodega pendiente", cuerpo, estado="WARN")
    _enviar_a_lista(sorted(destinatarios), cuerpo)


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
            log_envio_wa(env_k, n, ok, msg)

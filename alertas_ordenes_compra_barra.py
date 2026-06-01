"""
Alertas automáticas: órdenes de compra sugeridas para barra (BOD-002).

Usa generar_ordenes_compra.py (Tipo=Barra, PAR, ventana_pedido).
Cantidades en botellas / ml según catálogo.

Variables (.env):
  TATAMI_ALERT_ORDENES_COMPRA_BARRA=1     Activa envío
  ALERTA_WA_ORDENES_BARRA                 Lista separada por comas (prioridad)
  Si vacío: ALERTA_WA_FELIPE + ALERTA_WA_MOISES + ALERTA_WA_EDUARDO
"""

from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv

load_dotenv(override=True)


def alertas_ordenes_compra_barra_habilitadas() -> bool:
    v = (os.getenv("TATAMI_ALERT_ORDENES_COMPRA_BARRA") or "").strip().lower()
    return v in ("1", "true", "yes", "si", "sí")


def destinatarios_ordenes_compra_barra() -> list[tuple[str, str]]:
    """(número WA, etiqueta log)."""
    preview = (os.getenv("ALERTA_WA_ORDENES_BARRA_PREVIEW") or "").strip()
    if preview:
        return [(preview, "preview órdenes barra")]

    lista = (os.getenv("ALERTA_WA_ORDENES_BARRA") or "").strip()
    if lista:
        out: list[tuple[str, str]] = []
        for raw in lista.split(","):
            n = raw.strip()
            if n:
                out.append((n, f"ordenes …{n[-4:]}"))
        return out

    out = []
    for var, label in (
        ("ALERTA_WA_FELIPE", "Felipe"),
        ("ALERTA_WA_MOISES", "Moisés"),
        ("ALERTA_WA_EDUARDO", "Eduardo"),
    ):
        n = (os.getenv(var) or "").strip()
        if n:
            out.append((n, label))
    return out


def _formatear_bloque_revision(ordenes: list[dict], hoy: date) -> list[str]:
    from generar_ordenes_compra import proveedor_activo_hoy

    bloques = [
        "🛒 Órdenes compra BARRA (revisión)",
        f"Fecha: {hoy.strftime('%d/%m/%Y')} | Bodega BOD-002",
        "Unidades: botellas / ml (estándar barra)",
        "⚠️ Solo revisión — validar antes de enviar al proveedor.",
        "",
    ]
    for oc in ordenes:
        prov = oc["proveedor"]
        ventana = prov.get("ventana_pedido", "")
        if proveedor_activo_hoy(ventana, hoy):
            tag = "ventana HOY"
        elif ventana:
            tag = f"ventana {ventana}"
        else:
            tag = "sin ventana"
        bloques.append(f"━━ {prov['razon_social']} ({tag}) ━━")
        for ln in oc["lineas"]:
            desc = (ln.get("descripcion_proveedor") or ln.get("nombre_mp", ""))[:34]
            cant = (ln.get("texto_cantidad") or "").strip()
            ub = (ln.get("unidad_base") or "").strip()
            bloques.append(
                f"• {desc}\n  Pedir: {cant}\n  "
                f"Stock {ln.get('stock_actual')} / PAR {ln.get('par_level')} {ub}"
            )
        bloques.append("")
        bloques.append("Msg proveedor:")
        bloques.append(oc.get("mensaje_whatsapp", ""))
        bloques.append("")
    return bloques


def _partir_mensajes(bloques: list[str], max_len: int = 4000) -> list[str]:
    """Divide en varios WA si supera límite Meta."""
    partes: list[str] = []
    actual: list[str] = []
    n = 0
    for b in bloques:
        chunk = b + "\n"
        if actual and sum(len(x) for x in actual) + len(chunk) > max_len:
            partes.append("\n".join(actual).strip())
            actual = []
            n += 1
        actual.append(b if b else "")
    if actual:
        partes.append("\n".join(actual).strip())
    if len(partes) > 1:
        return [
            f"{p}\n\n— parte {i + 1}/{len(partes)} —"
            for i, p in enumerate(partes)
        ]
    return partes


def enviar_alertas_ordenes_compra_barra(
    *,
    origen: str = "pipeline",
    sin_ventana: bool = False,
    hoy: date | None = None,
) -> dict:
    """
    Genera órdenes barra y envía WA a revisores configurados.
    Retorna resumen {ordenes, proveedores, lineas, enviados, omitido}.
    """
    hoy = hoy or date.today()
    res: dict = {
        "ordenes": 0,
        "proveedores": 0,
        "lineas": 0,
        "enviados": 0,
        "fallos": 0,
        "omitido": None,
    }

    if not alertas_ordenes_compra_barra_habilitadas():
        res["omitido"] = "TATAMI_ALERT_ORDENES_COMPRA_BARRA no activo"
        return res

    destinos = destinatarios_ordenes_compra_barra()
    if not destinos:
        res["omitido"] = "sin destinatarios (ALERTA_WA_ORDENES_BARRA o Felipe/Moisés/Eduardo)"
        print("  WA [OMITIDO] órdenes barra: sin destinatarios")
        return res

    from generar_ordenes_compra import generar_ordenes

    ordenes = generar_ordenes(tipo="barra", sin_ventana=sin_ventana, hoy=hoy)
    res["ordenes"] = len(ordenes)
    res["proveedores"] = len(ordenes)
    res["lineas"] = sum(o.get("n_items", 0) for o in ordenes)

    if not ordenes:
        res["omitido"] = "sin ítems bajo PAR"
        return res

    bloques = _formatear_bloque_revision(ordenes, hoy)
    if origen:
        bloques.insert(1, f"Origen: {origen}")
    mensajes = _partir_mensajes(bloques)

    from alertas_tatami import enviar_alerta, enviar_whatsapp_texto, log_envio_wa

    for cuerpo in mensajes:
        enviar_alerta("Órdenes compra barra", cuerpo, estado="INFO")
        for numero, etiqueta in destinos:
            ok, msg = enviar_whatsapp_texto(numero, cuerpo)
            log_envio_wa(f"{etiqueta} órdenes barra", numero, ok, msg)
            if ok:
                res["enviados"] += 1
            else:
                res["fallos"] += 1

    print(
        f"  WA órdenes barra: {res['proveedores']} prov, {res['lineas']} líneas → "
        f"{len(destinos)} destinatario(s), {len(mensajes)} mensaje(s)"
    )
    return res


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Enviar alertas WA órdenes compra barra")
    ap.add_argument("--sin-ventana", action="store_true", help="Incluir todos los proveedores barra")
    args = ap.parse_args()
    r = enviar_alertas_ordenes_compra_barra(
        origen="manual", sin_ventana=args.sin_ventana
    )
    print(r)

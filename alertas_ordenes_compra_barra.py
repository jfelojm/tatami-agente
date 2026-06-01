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


def _nombre_legible_mp_anexo(f: dict) -> str | None:
    """Nombre del maestro; descripción de proveedor solo si no hay nombre útil."""
    cod = str(f.get("cod_mp_sistema", "")).strip()
    cod_norm = cod.lstrip("0") or cod

    nom = (f.get("nombre_mp") or "").strip()
    if nom:
        nom_norm = nom.lstrip("0") or nom
        if nom_norm != cod_norm:
            return nom[:44]

    desc = (f.get("descripcion_proveedor") or "").strip()
    if desc and not nom:
        return desc[:44]
    return None


def _etiqueta_linea_mp_anexo(f: dict) -> str:
    cod = str(f.get("cod_mp_sistema", "")).strip()
    nombre = _nombre_legible_mp_anexo(f)
    if nombre:
        return f"• MP {cod} — {nombre}"
    return f"• MP {cod}"


def _formatear_bloque_stock_cero(filas: list[dict]) -> list[str]:
    """Anexo: MPs con stock total en cero (barra / consignación). Lista completa."""
    if not filas:
        return []

    bloques = [
        "📋 ANEXO — MPs sin stock (total ≤ 0)",
        "Stock total = suma en todas las bodegas (002 barra, 003 consignación, etc.).",
        "Pedido propuesto solo si hay catálogo en BD_ITEMS_PROV (Barra → BOD-002).",
        "",
    ]
    for f in filas:
        ub = (f.get("unidad_base") or "").strip()
        par = f.get("par_level", 0)
        stk = f.get("stock_total", 0)
        bloques.append(_etiqueta_linea_mp_anexo(f))
        bloques.append(f"  Stock total: {stk} | PAR: {par} {ub}")
        pedido = (f.get("pedido_propuesto") or "").strip()
        if pedido:
            bloques.append(f"  Pedido sugerido: {pedido}")
        nota = (f.get("nota") or "").strip()
        if nota:
            bloques.append(f"  ({nota})")
        motivo = (f.get("motivo_sin_pedido") or "").strip()
        if motivo and not pedido:
            bloques.append(f"  Sin pedido automático: {motivo}")
        por = f.get("stock_por_bodega") or {}
        if por:
            desg = ", ".join(f"{b}:{v}" for b, v in sorted(por.items()))
            bloques.append(f"  Desglose: {desg}")
        bloques.append("")

    sin_pedido = sum(1 for f in filas if not f.get("pedido_propuesto"))
    bloques.append(
        f"Resumen anexo: {len(filas)} MPs sin stock y PAR > 0 (o con pedido sugerido) | "
        f"{sin_pedido} sin pedido automático (catálogo/ventana)."
    )
    return bloques


def _formatear_bloque_revision(ordenes: list[dict], hoy: date) -> list[str]:
    from generar_ordenes_compra import proveedor_activo_hoy

    bloques = [
        "🛒 Órdenes compra BARRA (revisión)",
        f"Fecha: {hoy.strftime('%d/%m/%Y')} | Ingreso BOD-002 | Stock vs PAR: todas las bodegas",
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

    from generar_ordenes_compra import generar_ordenes, listar_mp_stock_cero_para_alertas

    ordenes = generar_ordenes(tipo="barra", sin_ventana=sin_ventana, hoy=hoy)
    stock_cero = listar_mp_stock_cero_para_alertas(
        tipo="barra", sin_ventana=sin_ventana, hoy=hoy, ordenes=ordenes
    )
    res["ordenes"] = len(ordenes)
    res["proveedores"] = len(ordenes)
    res["lineas"] = sum(o.get("n_items", 0) for o in ordenes)
    res["mp_stock_cero"] = len(stock_cero)

    if not ordenes and not stock_cero:
        res["omitido"] = "sin ítems bajo PAR ni MPs en cero en barra/consignación"
        return res

    bloques: list[str] = []
    if origen:
        bloques.append(f"Origen: {origen}")
    if ordenes:
        bloques.extend(_formatear_bloque_revision(ordenes, hoy))
    else:
        bloques.extend(
            [
                "🛒 Órdenes compra BARRA (revisión)",
                f"Fecha: {hoy.strftime('%d/%m/%Y')}",
                "Sin líneas bajo PAR con catálogo y ventana hoy.",
                "",
            ]
        )
    if stock_cero:
        bloques.append("")
        bloques.extend(_formatear_bloque_stock_cero(stock_cero))

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
        f"  WA órdenes barra: {res['proveedores']} prov, {res['lineas']} líneas, "
        f"{res.get('mp_stock_cero', 0)} MPs stock cero → "
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

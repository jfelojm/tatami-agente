#!/usr/bin/env python3
"""Envía órdenes de compra barra por WhatsApp (solo dentro de ventana 24h).

Uso:
  1. Desde tu celular, envía cualquier mensaje a Tatami (+593 96 279 3109).
  2. Ejecuta: python entregar_ordenes_wa.py
  3. Presiona Enter cuando hayas enviado el mensaje.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from alertas_ordenes_compra_barra import (  # noqa: E402
    mensajes_ordenes_barra_por_proveedor,
)
from alertas_tatami import enviar_whatsapp_documento, enviar_whatsapp_texto  # noqa: E402
from generar_ordenes_compra import generar_ordenes  # noqa: E402


def main() -> None:
    import os

    destino = (os.getenv("ALERTA_WA_FELIPE") or "593987122959").strip()
    print(f"Destino: {destino}")
    print("Paso 1: Envía un mensaje (ej. 'hola' o 'pedidos barra') al WhatsApp Tatami +593 96 279 3109")
    input("Paso 2: Cuando lo hayas enviado, presiona Enter aquí... ")

    hoy = date.today()
    ordenes = generar_ordenes(tipo="barra", sin_ventana=True, hoy=hoy)
    if not ordenes:
        print("No hay órdenes en ORDENES_COMPRA.")
        return

    txt_path = Path("exports/ordenes_barra_22jun.txt")
    if txt_path.is_file():
        ok, msg = enviar_whatsapp_documento(
            destino,
            txt_path.read_bytes(),
            txt_path.name,
            caption=f"Órdenes barra {hoy}",
        )
        print(f"Documento: {ok} {msg}")

    intro = (
        f"Órdenes de compra BARRA ({hoy}). "
        f"{len(ordenes)} proveedor(es). Detalle por mensaje:"
    )
    ok, msg = enviar_whatsapp_texto(destino, intro)
    print(f"Intro: {ok} {msg}")
    if not ok:
        print("Falló intro — ¿ventana 24h abierta? Reintenta enviando 'hola' al bot.")
        return

    mensajes = mensajes_ordenes_barra_por_proveedor(ordenes, hoy)
    for i, m in enumerate(mensajes, 1):
        ok, det = enviar_whatsapp_texto(destino, m[:4000])
        print(f"  {i}/{len(mensajes)}: {ok} {det}")
        time.sleep(1.2)

    print("Listo. Si no ves nada en el teléfono, la ventana 24h sigue cerrada — reenvía 'hola' al bot.")


if __name__ == "__main__":
    main()

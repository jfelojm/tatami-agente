"""Envía preview órdenes barra + anexo stock cero solo a ALERTA_WA_FELIPE."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> int:
    from alertas_ordenes_compra_barra import (
        _formatear_bloque_revision,
        _formatear_bloque_stock_cero,
        _partir_mensajes,
    )
    from alertas_tatami import enviar_whatsapp_texto, log_envio_wa
    from generar_ordenes_compra import generar_ordenes, listar_mp_stock_cero_para_alertas

    hoy = date.today()
    sin_ventana = True
    ordenes = generar_ordenes(tipo="barra", sin_ventana=sin_ventana, hoy=hoy)
    stock_cero = listar_mp_stock_cero_para_alertas(
        tipo="barra", sin_ventana=sin_ventana, hoy=hoy, ordenes=ordenes
    )

    bloques = ["Origen: revisión manual (solo tú — preview)"]
    if ordenes:
        bloques.extend(_formatear_bloque_revision(ordenes, hoy))
    else:
        bloques.extend(
            [
                "🛒 Órdenes compra BARRA (revisión)",
                f"Fecha: {hoy.strftime('%d/%m/%Y')}",
                "Sin líneas bajo PAR con catálogo.",
                "",
            ]
        )
    if stock_cero:
        bloques.append("")
        bloques.extend(_formatear_bloque_stock_cero(stock_cero))

    mensajes = _partir_mensajes(bloques)
    numero = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
    if not numero:
        print("ERROR: ALERTA_WA_FELIPE no está en .env")
        return 1

    out = Path("logs/preview_ordenes_barra_wa.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for i, m in enumerate(mensajes, 1):
            f.write(f"=== parte {i}/{len(mensajes)} ===\n{m}\n\n")

    print(f"Copia local: {out.resolve()}")
    print(f"Enviando {len(mensajes)} mensaje(s) solo a …{numero[-4:]}")

    ok_all = True
    for i, cuerpo in enumerate(mensajes, 1):
        ok, msg = enviar_whatsapp_texto(numero, cuerpo)
        log_envio_wa(f"preview ordenes barra {i}/{len(mensajes)}", numero, ok, msg)
        print(f"  Parte {i}: {'OK' if ok else 'FALLO'} {msg[:100]}")
        ok_all = ok_all and ok

    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())

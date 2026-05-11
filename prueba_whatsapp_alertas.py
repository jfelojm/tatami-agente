"""
Prueba directa: envía un WhatsApp corto a Felipe y Moisés (variables ALERTA_WA_* en .env).

Uso (desde tatami-agente):
  python prueba_whatsapp_alertas.py

Requisitos en .env:
  WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN
  ALERTA_WA_FELIPE, ALERTA_WA_MOISES (solo dígitos, ej. 5939...)
  NO pongas TATAMI_WA_SKIP=1 si querés que realmente envíe.

Si Meta rechaza: abrí chat con el número de negocio desde cada teléfono al menos una vez,
o usá una plantilla aprobada para mensajes fuera de la ventana 24h.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(override=True)

MSG = "Prueba Tatami — alertas. Si ves esto, el envío desde el PC OK. Ignorar."


def main() -> None:
    from alertas_tatami import enviar_whatsapp_texto

    skip = (os.getenv("TATAMI_WA_SKIP") or "").strip()
    if skip.lower() in ("1", "true", "si", "sí", "yes"):
        print("ERROR: TATAMI_WA_SKIP está activo — quitá esa línea del .env o ponela en 0 para esta prueba.")
        raise SystemExit(1)

    pid = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    tok = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not pid or not tok:
        print("ERROR: faltan WHATSAPP_PHONE_NUMBER_ID o WHATSAPP_ACCESS_TOKEN en .env")
        raise SystemExit(1)

    destinos = [
        ("ALERTA_WA_FELIPE", os.getenv("ALERTA_WA_FELIPE") or ""),
        ("ALERTA_WA_MOISES", os.getenv("ALERTA_WA_MOISES") or ""),
    ]

    for nombre_env, num in destinos:
        if not num.strip():
            print(f"WARN: {nombre_env} vacío — no se envía a ese contacto.")
            continue
        ok, detalle = enviar_whatsapp_texto(num, MSG)
        estado = "OK" if ok else "FALLO"
        print(f"  [{estado}] {nombre_env} ... {detalle}")

    print("\nListo. Si sale FALLO, copiá el mensaje de error; suele ser token, número o política de Meta (24h / plantilla).")


if __name__ == "__main__":
    main()

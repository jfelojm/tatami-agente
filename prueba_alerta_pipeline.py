"""Ejecutar: python prueba_alerta_pipeline.py (desde tatami-agente, con .env cargado)."""

from alertas_pipeline import alerta_fallo

if __name__ == "__main__":
    alerta_fallo(
        modulo="reconciliar_ventas",
        razon="Prueba de alerta — ignorar",
        pasos_ejecutados=["ventas_smartmenu"],
        pasos_pendientes=[
            "descargo_inventario",
            "procesar_facturas",
            "recalcular_stock",
            "calcular_par_levels",
        ],
        fecha="2026-05-10",
    )
    print("Listo: revisá log, webhook y WhatsApp (si no está TATAMI_WA_SKIP=1).")

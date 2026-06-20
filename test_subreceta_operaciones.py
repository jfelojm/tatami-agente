"""Formato WhatsApp producción subreceta — nombre + código."""

from __future__ import annotations

import unittest

from subreceta_operaciones import _etiqueta_mp, _etiqueta_sub, _formatear_plan_wa


class TestEtiquetasProduccion(unittest.TestCase):
    def test_etiqueta_mp_con_nombre(self):
        t = _etiqueta_mp(
            {"cod_mp_sistema": "103", "nombre_mp": "COSTILLA DE CERDO"},
            nombres_mp={"103": "COSTILLA DE CERDO"},
        )
        self.assertEqual(t, "COSTILLA DE CERDO (103)")

    def test_etiqueta_mp_lookup_maestro(self):
        t = _etiqueta_mp(
            {"cod_mp_sistema": "103", "nombre_mp": "103"},
            nombres_mp={"103": "COSTILLA DE CERDO"},
        )
        self.assertEqual(t, "COSTILLA DE CERDO (103)")

    def test_etiqueta_sub_sin_doble_sub(self):
        t = _etiqueta_sub("SUB-056", "costillas char siu produccion")
        self.assertEqual(t, "costillas char siu produccion (SUB-056)")

    def test_formatear_plan_incluye_nombres(self):
        plan = {
            "cod_subreceta": "SUB-056",
            "nombre_subreceta": "costillas char siu produccion",
            "cantidad_producida": 100.0,
            "unidad": "gr",
            "rendimiento_estandar": 16983.0,
            "factor": 0.005888,
            "bodega_destino": "BOD-001",
            "entrada_sub": {
                "cod_mp_sistema": "SUB-056",
                "nombre_mp": "costillas char siu produccion",
                "cantidad_mov": 100.0,
                "unidad_base": "gr",
            },
            "salidas_mp": [
                {
                    "cod_mp_sistema": "103",
                    "nombre_mp": "COSTILLA DE CERDO",
                    "cod_bodega": "BOD-005",
                    "cantidad_mov": 100.0,
                    "unidad_base": "gr",
                }
            ],
            "avisos": [],
        }
        txt = _formatear_plan_wa(
            plan,
            simular=True,
            nombres_mp={"103": "COSTILLA DE CERDO"},
        )
        self.assertIn("costillas char siu produccion (SUB-056)", txt)
        self.assertNotIn("SUB SUB-056", txt)
        self.assertIn("COSTILLA DE CERDO (103)", txt)


if __name__ == "__main__":
    unittest.main()

"""Política de mínimo físico de botellas en pedidos de barra."""

from __future__ import annotations

import unittest

from generar_ordenes_compra import (
    aplicar_stock_minimo_botellas,
    enriquecer_linea_unidades_barra,
)
from inventario_stock_mp import agrupar_stock_par_por_mp


class TestStockMinimoBotellas(unittest.TestCase):
    def test_agrupa_politica_por_mp(self):
        rows = [
            {
                "cod_mp_sistema": "298",
                "cod_bodega": "BOD-002",
                "stock_actual": "1470",
                "par_level": "345.68",
                "stock_minimo_botellas": "2",
                "activa": "SI",
            }
        ]
        info = agrupar_stock_par_por_mp(rows)["298"]
        self.assertEqual(info["stock_minimo_botellas"], 2)

    def test_ml_pide_una_botella_para_llegar_a_dos(self):
        mp = {
            "stock_actual": 1470,
            "stock_botella": 1470,
            "par_level": 345.68,
            "cantidad_base": 0,
            "cantidad_base_par": 0,
            "stock_minimo_botellas": 2,
        }
        item = {"factor_conversion": 750}
        out = aplicar_stock_minimo_botellas(mp, item, tipo="barra")
        self.assertEqual(out["stock_minimo_base"], 1500)
        self.assertEqual(out["cantidad_base_minimo"], 30)
        self.assertEqual(out["cantidad_base"], 30)
        self.assertTrue(out["motivo_stock_minimo"])

    def test_uni_pide_una_botella_si_hay_una(self):
        mp = {
            "stock_actual": 1,
            "stock_botella": 1,
            "par_level": 0.2,
            "cantidad_base": 0,
            "cantidad_base_par": 0,
            "stock_minimo_botellas": 2,
        }
        out = aplicar_stock_minimo_botellas(
            mp, {"factor_conversion": 1}, tipo="barra"
        )
        self.assertEqual(out["cantidad_base"], 1)

    def test_par_mayor_que_minimo_prevalece(self):
        mp = {
            "stock_actual": 500,
            "stock_botella": 500,
            "par_level": 2500,
            "cantidad_base": 2000,
            "cantidad_base_par": 2000,
            "stock_minimo_botellas": 2,
        }
        out = aplicar_stock_minimo_botellas(
            mp, {"factor_conversion": 750}, tipo="barra"
        )
        self.assertEqual(out["cantidad_base_minimo"], 1000)
        self.assertEqual(out["cantidad_base"], 2000)
        self.assertFalse(out["motivo_stock_minimo"])

    def test_batch_no_cuenta_como_botella_fisica(self):
        mp = {
            "stock_actual": 2300,
            "stock_botella": 800,
            "stock_en_batch": 1500,
            "par_level": 1000,
            "cantidad_base": 0,
            "cantidad_base_par": 0,
            "stock_minimo_botellas": 2,
        }
        out = aplicar_stock_minimo_botellas(
            mp, {"factor_conversion": 750}, tipo="barra"
        )
        self.assertEqual(out["cantidad_base"], 700)

    def test_no_aplica_fuera_de_barra(self):
        mp = {
            "stock_actual": 0,
            "cantidad_base": 0,
            "stock_minimo_botellas": 2,
        }
        out = aplicar_stock_minimo_botellas(
            mp, {"factor_conversion": 750}, tipo="cocina"
        )
        self.assertEqual(out["cantidad_base"], 0)
        self.assertNotIn("stock_minimo_base", out)

    def test_vino_ice_se_presenta_como_botella(self):
        linea = {
            "nombre_mp": "Vino JP Chenet Ice Edition",
            "unidad_base": "uni",
            "cantidad_base": 1,
            "factor_conversion": 1,
            "stock_minimo_botellas": 2,
        }
        item = {
            "descripcion_proveedor": "JP CHENET ICE SPARKLING",
            "unidad_compra": "uni",
            "factor_conversion": 1,
        }
        enriquecer_linea_unidades_barra(linea, item)
        self.assertEqual(linea["texto_cantidad"], "1 botella")


if __name__ == "__main__":
    unittest.main()

"""Déficit de batches barra → demanda extra de botellas en órdenes."""

from __future__ import annotations

import unittest
from unittest.mock import patch


class TestDemandaBatchOrdenes(unittest.TestCase):
    def test_explota_deficit_batch_a_mps(self):
        from generar_ordenes_compra import demanda_mp_por_deficit_batches_barra

        rows = [
            {
                "cod_mp_sistema": "SUB-051",
                "nombre_mp": "Batch negroni",
                "unidad_base": "ml",
                "cod_bodega": "BOD-002",
                "stock_actual": "500",
                "par_level": "1500",
            },
            {
                "cod_mp_sistema": "196",
                "nombre_mp": "Gin",
                "unidad_base": "ml",
                "cod_bodega": "BOD-002",
                "stock_actual": "5000",
                "par_level": "3000",
            },
        ]
        # 1000 ml déficit × proporciones de receta
        mp_por_unidad = {"SUB-051": {"196": 0.4, "197": 0.3, "198": 0.3}}

        with patch(
            "subreceta_consumo_mp.cargar_mp_por_unidad_subreceta",
            return_value=mp_por_unidad,
        ):
            demanda, detalle = demanda_mp_por_deficit_batches_barra(rows)

        self.assertAlmostEqual(demanda["196"], 400.0)
        self.assertAlmostEqual(demanda["197"], 300.0)
        self.assertAlmostEqual(demanda["198"], 300.0)
        self.assertEqual(len(detalle), 1)
        self.assertEqual(detalle[0]["cod_subreceta"], "SUB-051")
        self.assertAlmostEqual(detalle[0]["deficit"], 1000.0)

    def test_sin_deficit_no_suma(self):
        from generar_ordenes_compra import demanda_mp_por_deficit_batches_barra

        rows = [
            {
                "cod_mp_sistema": "SUB-051",
                "nombre_mp": "Batch negroni",
                "unidad_base": "ml",
                "cod_bodega": "BOD-002",
                "stock_actual": "2000",
                "par_level": "1500",
            },
        ]
        with patch(
            "subreceta_consumo_mp.cargar_mp_por_unidad_subreceta",
            return_value={"SUB-051": {"196": 0.4}},
        ):
            demanda, detalle = demanda_mp_por_deficit_batches_barra(rows)
        self.assertEqual(demanda, {})
        self.assertEqual(detalle, [])

    def test_aplicar_suma_a_mp_ya_bajo_par(self):
        from generar_ordenes_compra import aplicar_demanda_batch_a_mps_bajo

        mps_bajo = {
            "196": {
                "cod_mp_sistema": "196",
                "nombre_mp": "Gin",
                "unidad_base": "ml",
                "stock_actual": 1000.0,
                "stock_por_bodega": {"BOD-002": 1000.0},
                "par_level": 3000.0,
                "cantidad_base": 2000.0,
                "cantidad_base_par": 2000.0,
                "cantidad_base_batch": 0.0,
                "cod_bodega": "BOD-002",
            }
        }
        with patch(
            "generar_ordenes_compra.demanda_mp_por_deficit_batches_barra",
            return_value=({"196": 400.0}, [{"cod_subreceta": "SUB-051", "deficit": 1000}]),
        ):
            detalle = aplicar_demanda_batch_a_mps_bajo(mps_bajo, tipo="barra", rows=[])

        self.assertEqual(len(detalle), 1)
        # par_efectivo=3400 - stock 1000 = 2400
        self.assertAlmostEqual(mps_bajo["196"]["cantidad_base"], 2400.0)
        self.assertAlmostEqual(mps_bajo["196"]["cantidad_base_par"], 2000.0)
        self.assertAlmostEqual(mps_bajo["196"]["cantidad_base_batch"], 400.0)
        self.assertAlmostEqual(mps_bajo["196"]["par_level"], 3400.0)
        self.assertAlmostEqual(mps_bajo["196"]["par_level_botella"], 3000.0)

    def test_stock_cubre_par_mas_batch_no_pide(self):
        """Choya/Botran: stock >> PAR botella; batch no debe forzar pedido."""
        from generar_ordenes_compra import aplicar_demanda_batch_a_mps_bajo

        rows = [
            {
                "cod_mp_sistema": "196",
                "nombre_mp": "Sake Choya",
                "unidad_base": "ml",
                "cod_bodega": "BOD-002",
                "stock_actual": "2665",
                "par_level": "1505",
                "activa": "SI",
            }
        ]
        mps_bajo: dict = {}
        with patch(
            "generar_ordenes_compra.demanda_mp_por_deficit_batches_barra",
            return_value=({"196": 258.0}, [{"cod_subreceta": "SUB-052"}]),
        ):
            aplicar_demanda_batch_a_mps_bajo(mps_bajo, tipo="barra", rows=rows)

        self.assertNotIn("196", mps_bajo)

    def test_stock_entre_par_y_par_batch_pide_solo_faltante(self):
        from generar_ordenes_compra import aplicar_demanda_batch_a_mps_bajo

        rows = [
            {
                "cod_mp_sistema": "196",
                "nombre_mp": "Sake",
                "unidad_base": "ml",
                "cod_bodega": "BOD-002",
                "stock_actual": "1600",
                "par_level": "1500",
                "activa": "SI",
            }
        ]
        mps_bajo: dict = {}
        with patch(
            "generar_ordenes_compra.demanda_mp_por_deficit_batches_barra",
            return_value=({"196": 300.0}, [{"cod_subreceta": "SUB-052"}]),
        ):
            aplicar_demanda_batch_a_mps_bajo(mps_bajo, tipo="barra", rows=rows)

        # par_efectivo=1800 - 1600 = 200
        self.assertIn("196", mps_bajo)
        self.assertAlmostEqual(mps_bajo["196"]["cantidad_base"], 200.0)
        self.assertAlmostEqual(mps_bajo["196"]["par_level"], 1800.0)


if __name__ == "__main__":
    unittest.main()

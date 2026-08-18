"""Stock total multi-bodega vs PAR global."""

from __future__ import annotations

import unittest

from inventario_stock_mp import agrupar_stock_par_por_mp, mps_bajo_par


class TestInventarioStockMp(unittest.TestCase):
    def test_suma_stock_todas_bodegas(self):
        rows = [
            {
                "cod_mp_sistema": "566",
                "cod_bodega": "BOD-002",
                "stock_actual": 1000,
                "par_level": 5000,
                "nombre_mp": "Buchanan",
                "unidad_base": "ml",
            },
            {
                "cod_mp_sistema": "566",
                "cod_bodega": "BOD-003",
                "stock_actual": 4500,
                "par_level": 5000,
                "nombre_mp": "Buchanan",
                "unidad_base": "ml",
            },
        ]
        g = agrupar_stock_par_por_mp(rows)
        self.assertAlmostEqual(g["566"]["stock_total"], 5500.0)
        self.assertFalse(g["566"]["bajo_par"])

    def test_bajo_par_si_suma_insuficiente(self):
        rows = [
            {"cod_mp_sistema": "176", "cod_bodega": "BOD-002", "stock_actual": 200, "par_level": 1000},
            {"cod_mp_sistema": "176", "cod_bodega": "BOD-003", "stock_actual": 300, "par_level": 1000},
        ]
        bajo = mps_bajo_par(rows, incluir_equiv_batches_barra=False)
        self.assertIn("176", bajo)
        self.assertAlmostEqual(bajo["176"]["stock_total"], 500.0)
        self.assertAlmostEqual(bajo["176"]["cantidad_faltante"], 500.0)

    def test_nombre_mp_aunque_par_cero(self):
        rows = [
            {
                "cod_mp_sistema": "170",
                "cod_bodega": "BOD-002",
                "stock_actual": 0,
                "par_level": 0,
                "nombre_mp": "Finestcall Elderflower",
                "unidad_base": "ml",
            },
        ]
        g = agrupar_stock_par_por_mp(rows)
        self.assertEqual(g["170"]["nombre_mp"], "Finestcall Elderflower")

    def test_ignora_bodega_inactiva(self):
        rows = [
            {"cod_mp_sistema": "001", "cod_bodega": "BOD-002", "stock_actual": 10, "par_level": 100},
            {"cod_mp_sistema": "001", "cod_bodega": "BOD-004", "stock_actual": 999, "par_level": 100},
        ]
        g = agrupar_stock_par_por_mp(rows)
        self.assertAlmostEqual(g["001"]["stock_total"], 10.0)

    def test_mp_inactiva_no_entra_bajo_par(self):
        rows = [
            {
                "cod_mp_sistema": "219",
                "cod_bodega": "BOD-002",
                "stock_actual": 0,
                "par_level": 233,
                "nombre_mp": "Vino Partager Blanc",
                "activa": "NO",
            },
        ]
        g = agrupar_stock_par_por_mp(rows)
        self.assertFalse(g["219"]["activa"])
        self.assertTrue(g["219"]["bajo_par"])
        self.assertNotIn("219", mps_bajo_par(rows, incluir_equiv_batches_barra=False))
        self.assertIn(
            "219",
            mps_bajo_par(rows, solo_mps_activas=False, incluir_equiv_batches_barra=False),
        )

    def test_stock_equiv_batch_evita_bajo_par(self):
        from unittest.mock import patch

        rows = [
            {
                "cod_mp_sistema": "196",
                "cod_bodega": "BOD-002",
                "stock_actual": 2000,
                "par_level": 2500,
                "nombre_mp": "Sake",
                "activa": "SI",
            },
            {
                "cod_mp_sistema": "SUB-052",
                "cod_bodega": "BOD-002",
                "stock_actual": 2000,
                "par_level": 1800,
                "nombre_mp": "tokio",
                "activa": "SI",
            },
        ]
        # 2000 ml batch * 0.4 sake/ml = 800 → stock ef 2800 >= 2500
        with patch(
            "subreceta_consumo_mp.cargar_mp_por_unidad_subreceta",
            return_value={"SUB-052": {"196": 0.4}},
        ):
            bajo = mps_bajo_par(rows, incluir_equiv_batches_barra=True)
        self.assertNotIn("196", bajo)


if __name__ == "__main__":
    unittest.main()

"""Costo por línea en BD_RECETAS_DETALLE (MP y SUB)."""

import unittest

from calcular_costo_recetas import costo_linea_receta
from recetas_detalle import es_linea_mp


class TestCostoRecetaDetalle(unittest.TestCase):
    def test_mp_con_costo(self):
        ln = {
            "cod_mp_sistema": "004",
            "cod_subreceta": "",
            "cantidad": "100",
            "cod_bodega": "BOD-001",
            "merma_pct": "0",
            "pct_aplicacion": "1",
        }
        self.assertTrue(es_linea_mp(ln))
        costos = {("4", "BOD-001"): 0.05}
        det = costo_linea_receta(ln, costos, {})
        self.assertIsNotNone(det)
        assert det is not None
        self.assertEqual(det["costo_unitario"], 0.05)
        self.assertEqual(det["costo_linea"], 5.0)
        self.assertEqual(det.get("nota"), "")

    def test_mp_sin_costo(self):
        ln = {
            "cod_mp_sistema": "999",
            "cod_subreceta": "",
            "cantidad": "10",
            "cod_bodega": "BOD-002",
            "merma_pct": "0",
            "pct_aplicacion": "1",
        }
        det = costo_linea_receta(ln, {}, {})
        self.assertIsNotNone(det)
        assert det is not None
        self.assertEqual(det["costo_linea"], 0.0)
        self.assertIn("sin_costo", det.get("nota", ""))

    def test_sub_sin_costo(self):
        ln = {
            "cod_mp_sistema": "",
            "cod_subreceta": "050",
            "cantidad": "30",
            "pct_aplicacion": "1",
        }
        det = costo_linea_receta(ln, {}, {})
        self.assertIsNotNone(det)
        assert det is not None
        self.assertEqual(det["tipo"], "SUB")
        self.assertIn("sin_costo", det.get("nota", ""))


if __name__ == "__main__":
    unittest.main()

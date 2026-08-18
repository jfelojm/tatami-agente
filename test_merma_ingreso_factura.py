"""Tests: merma_pct_ingreso al registrar ENTRADA desde factura."""

from __future__ import annotations

import unittest

from procesar_facturas_drive import (
    _parse_merma_pct_ingreso,
    calcular_entrada_desde_factura,
    factor_neto_ingreso,
)


class TestMermaIngresoFactura(unittest.TestCase):
    def test_parse_merma_decimal_y_porcentaje(self):
        self.assertEqual(_parse_merma_pct_ingreso("0.15"), 0.15)
        self.assertEqual(_parse_merma_pct_ingreso("15"), 0.15)
        self.assertEqual(_parse_merma_pct_ingreso(""), 0.0)

    def test_factor_neto_15pct(self):
        prov = {"merma_pct_ingreso": "0.15"}
        self.assertAlmostEqual(factor_neto_ingreso(prov), 0.85)

    def test_entrada_10kg_merma_15(self):
        item_factura = {
            "cantidad": 10.0,
            "costo_efectivo": 22.0,
            "precio_total_sin_impuesto": 220.0,
        }
        item_prov = {"merma_pct_ingreso": "0.15"}
        factor = 1000.0  # kg -> g
        neto, costo_u, bruto = calcular_entrada_desde_factura(
            item_factura, item_prov, factor
        )
        self.assertAlmostEqual(bruto, 10000.0)
        self.assertAlmostEqual(neto, 8500.0)
        self.assertAlmostEqual(costo_u, 22.0 / (1000.0 * 0.85), places=6)
        self.assertAlmostEqual(neto * costo_u, 220.0, places=2)

    def test_sin_merma_igual_que_antes(self):
        item_factura = {"cantidad": 5.0, "costo_efectivo": 18.0}
        item_prov = {}
        factor = 1000.0
        neto, costo_u, bruto = calcular_entrada_desde_factura(
            item_factura, item_prov, factor
        )
        self.assertAlmostEqual(neto, 5000.0)
        self.assertAlmostEqual(bruto, 5000.0)
        self.assertAlmostEqual(costo_u, 0.018)


if __name__ == "__main__":
    unittest.main()

"""Reglas de bodega destino en entradas por factura."""

import unittest

from bodegas_config import resolver_bodega_entrada_linea


class TestBodegaEntradaFactura(unittest.TestCase):
    def test_catalogo_consignacion_redirige_a_barra(self):
        item = {"cod_bodega_destino": "BOD-003"}
        bod, err = resolver_bodega_entrada_linea(item)
        self.assertIsNone(err)
        self.assertEqual(bod, "BOD-002")

    def test_catalogo_barra_se_mantiene(self):
        item = {"cod_bodega_destino": "BOD-002"}
        bod, err = resolver_bodega_entrada_linea(item)
        self.assertEqual(bod, "BOD-002")

    def test_catalogo_cocina_se_mantiene(self):
        item = {"cod_bodega_destino": "BOD-001"}
        bod, err = resolver_bodega_entrada_linea(item)
        self.assertEqual(bod, "BOD-001")


if __name__ == "__main__":
    unittest.main()

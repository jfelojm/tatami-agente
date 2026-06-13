"""Delta de conteo físico vs saldo mov_inventario (no Sheets)."""

import unittest

from conteo_fisico import delta_conteo_vs_mov, stock_mov_mp_bodega


class TestConteoDeltaMov(unittest.TestCase):
    def test_delta_caso_mp166(self):
        # Conteo 680 ml, saldo mov 415 ml → ajuste +265 (no +985 por Sheets desfasado).
        self.assertEqual(delta_conteo_vs_mov(680.0, 415.0), 265.0)

    def test_stock_mov_mp_bodega_clave_normalizada(self):
        stock_map = {("166", "BOD-002"): 415.0}
        self.assertEqual(stock_mov_mp_bodega(stock_map, "166", "BOD-002"), 415.0)
        self.assertEqual(stock_mov_mp_bodega(stock_map, "166", "bod-002"), 415.0)

    def test_stock_mov_sin_movimientos_es_cero(self):
        self.assertEqual(stock_mov_mp_bodega({}, "301", "BOD-002"), 0.0)


if __name__ == "__main__":
    unittest.main()

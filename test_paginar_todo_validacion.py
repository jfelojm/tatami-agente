import unittest

from recalcular_stock_sheets import _validar_paginacion


class TestValidarPaginacion(unittest.TestCase):
    def test_ok(self):
        _validar_paginacion("mov_inventario", "cod_mov", [{"cod_mov": "A"}, {"cod_mov": "B"}], 2)

    def test_filas_nuevas_durante_lectura_ok(self):
        _validar_paginacion("mov_inventario", "cod_mov", [{"cod_mov": "A"}, {"cod_mov": "B"}], 1)

    def test_repetidas(self):
        with self.assertRaises(RuntimeError):
            _validar_paginacion("mov_inventario", "cod_mov", [{"cod_mov": "A"}, {"cod_mov": "A"}], 2)

    def test_omitidas(self):
        with self.assertRaises(RuntimeError):
            _validar_paginacion("mov_inventario", "cod_mov", [{"cod_mov": "A"}], 2)


if __name__ == "__main__":
    unittest.main()

import unittest

from subrecetas_bodegas_stock import (
    BODEGAS_SUB_BARRA,
    BODEGAS_SUB_COCINA,
    SUBRECETAS_BARRA,
    bodegas_para_subreceta,
)


class TestSubrecetasBodegasStock(unittest.TestCase):
    def test_barra_batches_solo_bod_002(self):
        for cod in ("051", "SUB-051", "052", "SUB-054"):
            bods = bodegas_para_subreceta(cod)
            self.assertEqual(bods, BODEGAS_SUB_BARRA)

    def test_cocina_bod_001_y_005(self):
        bods = bodegas_para_subreceta("SUB-006")
        self.assertEqual(bods, BODEGAS_SUB_COCINA)

    def test_cuatro_batches_barra(self):
        self.assertEqual(len(SUBRECETAS_BARRA), 4)


if __name__ == "__main__":
    unittest.main()

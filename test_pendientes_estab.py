"""BD_ITEMS_PENDIENTES: estab, formato_compra y fórmulas ES."""

import unittest

from proveedor_favorita import estab_desde_num_factura, formato_compra_desde_estab
from sheets_formulas_es import (
    formula_pendientes_link_xml,
    formula_pendientes_nombre_mp,
    formula_pendientes_ref_columna,
)


class TestPendientesEstab(unittest.TestCase):
    def test_estab_desde_num(self):
        self.assertEqual(estab_desde_num_factura("219-102-000069896"), "219")
        self.assertEqual(estab_desde_num_factura("016-107-000461066"), "016")

    def test_formato_compra(self):
        self.assertEqual(formato_compra_desde_estab("219"), "TITAN")
        self.assertEqual(formato_compra_desde_estab("016"), "SUPERMAXI")

    def test_formulas_usan_separador_es(self):
        f = formula_pendientes_nombre_mp("P", "B", "A", 5)
        self.assertIn(";", f)
        self.assertIn("SI(", f)
        self.assertIn("SI.ERROR", f)
        self.assertIn("INDICE", f)
        self.assertIn("COINCIDIR", f)
        self.assertNotIn("IFERROR", f)

    def test_formula_link_hipervinculo(self):
        f = formula_pendientes_link_xml("N", 3)
        self.assertIn("HIPERVINCULO", f)
        self.assertIn(";", f)

    def test_formula_ref_columna(self):
        self.assertEqual(formula_pendientes_ref_columna("I", 10), "=I10")


if __name__ == "__main__":
    unittest.main()

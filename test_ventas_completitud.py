import unittest

from ventas_completitud import (
    auditar_completitud,
    id_documento_desde_cod_venta,
    id_documentos_desde_grid_rows,
    mensaje_completitud,
)


class TestVentasCompletitud(unittest.TestCase):
    def test_id_documento_desde_cod_venta(self):
        self.assertEqual(
            id_documento_desde_cod_venta("VTA-20260530-7237-38908"),
            "7237",
        )
        self.assertEqual(id_documento_desde_cod_venta(""), "")
        self.assertEqual(id_documento_desde_cod_venta("INVALID"), "")

    def test_id_documentos_desde_grid_rows(self):
        rows = [
            ["7237", "6332", "FACTURA", "2026-05-30 23:21:07"],
            ["7240", "6335", "FACTURA", "2026-05-30 23:35:54"],
        ]
        self.assertEqual(id_documentos_desde_grid_rows(rows), {"7237", "7240"})

    def test_auditar_completitud_faltantes(self):
        class FakeSB:
            def table(self, name):
                self.name = name
                return self

            def select(self, cols):
                return self

            def eq(self, col, val):
                return self

            def range(self, a, b):
                return self

            def execute(self):
                return type(
                    "R",
                    (),
                    {
                        "data": [
                            {"cod_venta": "VTA-20260530-7191-38696"},
                            {"cod_venta": "VTA-20260530-7233-38895"},
                        ]
                    },
                )()

        rep = auditar_completitud(
            "2026-05-30",
            {"7191", "7233", "7237"},
            sb=FakeSB(),
        )
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["faltantes"], ["7237"])
        self.assertIn("7237", mensaje_completitud(rep))


if __name__ == "__main__":
    unittest.main()

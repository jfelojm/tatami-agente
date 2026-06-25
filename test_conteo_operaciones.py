"""Hojas de conteo por bodega."""

from __future__ import annotations

import unittest

from conteo_operaciones import sheet_name_por_bodega


class TestSheetNamePorBodega(unittest.TestCase):
    def test_cocina(self) -> None:
        self.assertEqual(sheet_name_por_bodega("BOD-001"), "CONTEO")

    def test_externa(self) -> None:
        self.assertEqual(sheet_name_por_bodega("BOD-005"), "CONTEO_EXTERNA")

    def test_barra(self) -> None:
        self.assertEqual(sheet_name_por_bodega("BOD-002"), "CONTEO_BARRA")


if __name__ == "__main__":
    unittest.main()

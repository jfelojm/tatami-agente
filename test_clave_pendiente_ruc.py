"""Claves de pendientes: RUC con/sin cero inicial deben coincidir."""

from __future__ import annotations

import unittest

from procesar_facturas_drive import _canonizar_clave_unica, _clave_item_pendiente
from codigo_factura_match import ruc_normalizado


class TestClavePendienteRuc(unittest.TestCase):
    def test_ruc_normalizado_zfill(self):
        self.assertEqual(ruc_normalizado("190170624001"), "0190170624001")
        self.assertEqual(ruc_normalizado("0190170624001"), "0190170624001")
        self.assertEqual(ruc_normalizado("'0190170624001"), "0190170624001")

    def test_clave_igual_con_y_sin_cero(self):
        item = {"cod_item_xml": "8000040002509"}
        k13 = _clave_item_pendiente(
            {"num_factura": "001-002-000200401", "ruc": "0190170624001"}, item
        )
        k12 = _clave_item_pendiente(
            {"num_factura": "001-002-000200401", "ruc": "190170624001"}, item
        )
        self.assertEqual(k13, k12)
        self.assertEqual(k13, "001-002-000200401|0190170624001|8000040002509")

    def test_canonizar_clave_unica_sheet(self):
        raw = "001-002-000200401|190170624001|8000040002509"
        self.assertEqual(
            _canonizar_clave_unica(raw),
            "001-002-000200401|0190170624001|8000040002509",
        )


if __name__ == "__main__":
    unittest.main()

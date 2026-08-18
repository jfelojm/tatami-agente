"""Tests deduplicación ENTRADA factura por ITEM_XML normalizado."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from procesar_facturas_drive import (
    _cod_item_xml_equivalente,
    _observaciones_tienen_item_xml_equivalente,
    mov_entrada_factura_linea_ya_registrada,
)


class TestItemXmlEquivalente(unittest.TestCase):
    def test_ceros_a_la_izquierda(self):
        self.assertTrue(
            _cod_item_xml_equivalente("000000000050002974", "50002974")
        )
        self.assertTrue(_cod_item_xml_equivalente("01014055", "1014055"))

    def test_distinto_item(self):
        self.assertFalse(_cod_item_xml_equivalente("50002974", "50002850"))

    def test_observaciones_padded_vs_corto(self):
        obs = "BUCHANANS | ORIGEN:XML | ITEM_XML:000000000050002974"
        self.assertTrue(_observaciones_tienen_item_xml_equivalente(obs, "50002974"))


class TestMovEntradaYaRegistrada(unittest.TestCase):
    @patch("procesar_facturas_drive.supabase")
    def test_detecta_duplicado_por_normalizacion(self, mock_sb):
        table = MagicMock()
        mock_sb.table.return_value = table
        chain = table.select.return_value
        chain.eq.return_value = chain
        chain.execute.return_value = MagicMock(
            data=[
                {
                    "observaciones": (
                        "WHISKY | ORIGEN:XML | ITEM_XML:000000000050002974"
                    )
                }
            ]
        )
        item = {"cod_item_xml": "50002974", "descripcion_proveedor": "WHISKY"}
        self.assertTrue(
            mov_entrada_factura_linea_ya_registrada("001-042-000244346", "158", item)
        )


if __name__ == "__main__":
    unittest.main()

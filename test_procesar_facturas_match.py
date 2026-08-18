"""Tests: buscar_item_prov no cruza proveedores por descripción."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from procesar_facturas_drive import buscar_item_prov


CATALOGO = [
    {
        "cod_proveedor": "006",
        "cod_item_prov": "01008009",
        "descripcion_proveedor": "LOMO FINO EMP. AL VACIO (K8101E)",
        "cod_mp_sistema": "552",
        "nombre_mp": "LOMO FINO DE RES PIGGIS",
        "precio_ref": "0.01799",
    },
    {
        "cod_proveedor": "068",
        "cod_item_prov": "000339",
        "descripcion_proveedor": "LOMO FINO DE RES KG",
        "cod_mp_sistema": "047",
        "nombre_mp": "LOMO FINO DE RES ITALIANA",
        "precio_ref": "0.022",
    },
    {
        "cod_proveedor": "091",
        "cod_item_prov": "DGER4",
        "descripcion_proveedor": "LOMO FINO",
        "cod_mp_sistema": "552",
        "nombre_mp": "LOMO FINO DE RES PIGGIS",
        "precio_ref": "0.0035",
        "activo": "NO",
    },
]

LOOKUP_RUC = {
    "0101298701001": "006",
    "0190343928001": "068",
    "0104680590001": "035",
    "0195114331001": "091",
}


class TestBuscarItemProv(unittest.TestCase):
    @patch("procesar_facturas_drive.cargar_lookup_ruc", return_value=LOOKUP_RUC)
    @patch("procesar_facturas_drive.cargar_bd_items_prov", return_value=CATALOGO)
    def test_pacheco_cod_item_ok(self, _items, _lookup):
        hit = buscar_item_prov(
            "0101298701001",
            "01008009",
            "LOMO FINO EMP. AL VACIO (K8101E)",
            "PACHECO VIDAL CARLOS GILBERTO",
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["cod_item_prov"], "01008009")
        self.assertEqual(hit["cod_mp_sistema"], "552")

    @patch("procesar_facturas_drive.cargar_lookup_ruc", return_value=LOOKUP_RUC)
    @patch("procesar_facturas_drive.cargar_bd_items_prov", return_value=CATALOGO)
    def test_lopez_castro_lomo_no_cruza_galabdistri(self, _items, _lookup):
        hit = buscar_item_prov(
            "0104680590001",
            "C004",
            "LOMO FINO",
            "LOPEZ CASTRO PABLO GEOVANNY",
        )
        self.assertIsNone(hit)

    @patch("procesar_facturas_drive.cargar_lookup_ruc", return_value=LOOKUP_RUC)
    @patch("procesar_facturas_drive.cargar_bd_items_prov", return_value=CATALOGO)
    def test_ruc_desconocido_no_match_por_descripcion(self, _items, _lookup):
        hit = buscar_item_prov(
            "0102101052001",
            "TR001",
            "LOMO FINO XG",
            "CASTRO PESANTEZ MARIANA DE JESUS",
        )
        self.assertIsNone(hit)

    @patch("procesar_facturas_drive.cargar_lookup_ruc", return_value=LOOKUP_RUC)
    @patch("procesar_facturas_drive.cargar_bd_items_prov", return_value=CATALOGO)
    def test_proveedor_sin_catalogo_no_match_descripcion_global(self, _items, _lookup):
        catalogo_sin_lopez = [it for it in CATALOGO if it["cod_proveedor"] != "035"]
        with patch(
            "procesar_facturas_drive.cargar_bd_items_prov",
            return_value=catalogo_sin_lopez,
        ):
            hit = buscar_item_prov(
                "0104680590001",
                "C004",
                "LOMO FINO",
                "LOPEZ CASTRO PABLO GEOVANNY",
            )
        self.assertIsNone(hit)


if __name__ == "__main__":
    unittest.main()

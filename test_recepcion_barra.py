"""Tests gate recepción Barra (sin Sheets/Supabase reales)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestBarraHelpers(unittest.TestCase):
    def test_barra_requiere_ok_default_on(self):
        with patch.dict(os.environ, {"SRI_BARRA_REQUIERE_OK": "1"}):
            from recepcion_compras_barra import barra_requiere_ok

            self.assertTrue(barra_requiere_ok())

    def test_barra_requiere_ok_off(self):
        with patch.dict(os.environ, {"SRI_BARRA_REQUIERE_OK": "0"}):
            from recepcion_compras_barra import barra_requiere_ok

            self.assertFalse(barra_requiere_ok())

    def test_clave_linea_cola(self):
        from recepcion_compras_barra import clave_linea_cola

        k = clave_linea_cola("001-001-1", "007")
        self.assertIn("001-001-1|", k)

    def test_es_proveedor_barra_por_ruc(self):
        import recepcion_compras_barra as m

        m._barra_cache = {"1792411149001"}
        m._barra_cod_cache = {"149"}
        self.assertTrue(m.es_proveedor_barra("1792411149001"))
        self.assertTrue(m.es_proveedor_barra("", "149"))
        self.assertFalse(m.es_proveedor_barra("9999999999999", "050"))


class TestGateProcesar(unittest.TestCase):
    def test_procesar_factura_dict_gate_barra_no_entrada(self):
        factura = {
            "num_factura": "001-002-999001",
            "ruc": "1792411149001",
            "razon_social": "ARCA CONTINENTAL",
            "fecha_factura": "2026-08-01",
            "total_sin_impuesto": 10.0,
            "items": [
                {
                    "linea": 1,
                    "cod_item_xml": "7",
                    "descripcion_proveedor": "COCA 3L",
                    "cantidad": 2,
                    "costo_efectivo": 5.0,
                    "precio_total_sin_impuesto": 10.0,
                }
            ],
        }
        item_prov = {
            "cod_mp_sistema": "MP-TEST",
            "nombre_mp": "Coca",
            "unidad_base_sistema": "uni",
            "unidad_compra": "caj",
            "factor_conversion": "24",
            "cod_bodega_destino": "BOD-002",
            "cod_proveedor": "149",
            "precio_ref": "0.2",
            "merma_pct_ingreso": "",
        }

        with (
            patch("procesar_facturas_drive.cargar_lookup_ruc", return_value={}),
            patch(
                "procesar_facturas_drive._cod_proveedor_desde_ruc", return_value="149"
            ),
            patch("procesar_facturas_drive.buscar_item_prov", return_value=item_prov),
            patch(
                "procesar_facturas_drive.mov_entrada_factura_linea_ya_registrada",
                return_value=False,
            ),
            patch(
                "procesar_facturas_drive.fecha_factura_permite_ingreso_stock",
                return_value=True,
            ),
            patch(
                "procesar_facturas_drive.conversion_compra_definida",
                return_value=(True, ""),
            ),
            patch("procesar_facturas_drive.procesar_variacion_precio"),
            patch("procesar_facturas_drive.registrar_entrada_inventario") as mock_ent,
            patch("recepcion_compras_barra.barra_requiere_ok", return_value=True),
            patch("recepcion_compras_barra.es_proveedor_barra", return_value=True),
            patch(
                "recepcion_compras_barra.encolar_lineas_por_recibir", return_value=1
            ) as mock_enc,
            patch(
                "bodegas_config.resolver_bodega_entrada_linea",
                return_value=("BOD-002", None),
            ),
        ):
            from procesar_facturas_drive import procesar_factura_dict

            r = procesar_factura_dict(factura, dry_run=False, origen="XML")

        self.assertEqual(r["estado"], "POR_RECIBIR")
        self.assertEqual(r["matcheados"], 1)
        self.assertEqual(r["por_recibir"], 1)
        mock_ent.assert_not_called()
        mock_enc.assert_called_once()

    def test_procesar_factura_dict_no_barra_si_entra(self):
        factura = {
            "num_factura": "001-002-999002",
            "ruc": "1790000000001",
            "razon_social": "COCINA PROV",
            "fecha_factura": "2026-08-01",
            "total_sin_impuesto": 10.0,
            "items": [
                {
                    "linea": 1,
                    "cod_item_xml": "1",
                    "descripcion_proveedor": "ARROZ",
                    "cantidad": 1,
                    "costo_efectivo": 10.0,
                    "precio_total_sin_impuesto": 10.0,
                }
            ],
        }
        item_prov = {
            "cod_mp_sistema": "MP-ARROZ",
            "nombre_mp": "Arroz",
            "unidad_base_sistema": "kg",
            "unidad_compra": "kg",
            "factor_conversion": "1",
            "cod_bodega_destino": "BOD-001",
            "cod_proveedor": "010",
            "precio_ref": "1",
            "merma_pct_ingreso": "",
        }

        with (
            patch("procesar_facturas_drive.cargar_lookup_ruc", return_value={}),
            patch(
                "procesar_facturas_drive._cod_proveedor_desde_ruc", return_value="010"
            ),
            patch("procesar_facturas_drive.buscar_item_prov", return_value=item_prov),
            patch(
                "procesar_facturas_drive.mov_entrada_factura_linea_ya_registrada",
                return_value=False,
            ),
            patch(
                "procesar_facturas_drive.fecha_factura_permite_ingreso_stock",
                return_value=True,
            ),
            patch(
                "procesar_facturas_drive.conversion_compra_definida",
                return_value=(True, ""),
            ),
            patch("procesar_facturas_drive.procesar_variacion_precio"),
            patch(
                "procesar_facturas_drive.registrar_entrada_inventario", return_value=True
            ) as mock_ent,
            patch("procesar_facturas_drive._flush_mp_sistema"),
            patch(
                "procesar_facturas_drive.calcular_entrada_desde_factura",
                return_value=(1.0, 10.0, 1.0),
            ),
            patch("recepcion_compras_barra.barra_requiere_ok", return_value=True),
            patch("recepcion_compras_barra.es_proveedor_barra", return_value=False),
            patch(
                "bodegas_config.resolver_bodega_entrada_linea",
                return_value=("BOD-001", None),
            ),
        ):
            from procesar_facturas_drive import procesar_factura_dict

            r = procesar_factura_dict(factura, dry_run=False, origen="XML")

        self.assertEqual(r["estado"], "COMPLETA")
        self.assertEqual(r.get("por_recibir", 0), 0)
        mock_ent.assert_called_once()


if __name__ == "__main__":
    unittest.main()

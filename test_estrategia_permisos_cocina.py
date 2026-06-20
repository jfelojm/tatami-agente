"""Permisos cocina: sin ventas/stock/compras; con traslado y producción."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import estrategia_config as ec
import wa_usabilidad as wu


JEFE_COCINA = "593992911956"
STAFF_COCINA = "593983242667"
JEFE_BARRA = "593999999001"


class TestPermisosCocina(unittest.TestCase):
    def setUp(self) -> None:
        ec._phone_to_roles.cache_clear()

    def _cfg_sin_bd(self):
        return patch("config_sheets.cfg", return_value=None)

    def test_jefe_cocina_sin_ventas_stock_compras(self) -> None:
        with self._cfg_sin_bd(), patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {"JEFE_COCINA": "ALLOWLIST_JEFE_COCINA"},
            clear=False,
        ), patch.dict(
            "os.environ",
            {"ALLOWLIST_JEFE_COCINA": JEFE_COCINA},
            clear=False,
        ):
            ec._phone_to_roles.cache_clear()
            tel = JEFE_COCINA
            self.assertFalse(ec.puede_consultar_ventas(tel))
            self.assertFalse(ec.puede_consultar_inventario(tel))
            self.assertFalse(ec.autorizado_tool(tel, "compras_facturas_rango"))
            self.assertFalse(ec.autorizado_tool(tel, "stock_critico"))
            self.assertTrue(ec.puede_trasladar(tel))

    def test_staff_cocina_sin_ventas_con_traslado(self) -> None:
        with self._cfg_sin_bd(), patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {"STAFF_COCINA": "ALLOWLIST_STAFF_COCINA"},
            clear=False,
        ), patch.dict(
            "os.environ",
            {"ALLOWLIST_STAFF_COCINA": STAFF_COCINA},
            clear=False,
        ):
            ec._phone_to_roles.cache_clear()
            tel = STAFF_COCINA
            self.assertFalse(ec.puede_consultar_ventas(tel))
            self.assertFalse(ec.puede_consultar_inventario(tel))
            self.assertTrue(ec.puede_trasladar(tel))
            self.assertTrue(ec.autorizado_tool(tel, "produccion_subreceta"))
            self.assertTrue(ec.autorizado_tool(tel, "listar_subrecetas"))

    def test_jefe_barra_mantiene_ventas_y_stock(self) -> None:
        with self._cfg_sin_bd(), patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {"JEFE_BARRA": "ALLOWLIST_JEFE_BARRA"},
            clear=False,
        ), patch.dict(
            "os.environ",
            {"ALLOWLIST_JEFE_BARRA": JEFE_BARRA},
            clear=False,
        ):
            ec._phone_to_roles.cache_clear()
            tel = JEFE_BARRA
            self.assertTrue(ec.puede_consultar_ventas(tel))
            self.assertTrue(ec.puede_consultar_inventario(tel))
            self.assertTrue(ec.puede_trasladar(tel))


    def test_staff_cocina_bloqueado_aunque_bd_tenga_ventas(self) -> None:
        """BD_CONFIG desactualizado no debe reabrir consultas a cocina."""
        with patch("config_sheets.cfg", return_value="ADMIN,SOCIO,JEFE_COCINA,STAFF_COCINA"), patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {"STAFF_COCINA": "ALLOWLIST_STAFF_COCINA"},
            clear=False,
        ), patch.dict(
            "os.environ",
            {"ALLOWLIST_STAFF_COCINA": STAFF_COCINA},
            clear=False,
        ):
            ec._phone_to_roles.cache_clear()
            self.assertFalse(ec.puede_consultar_ventas(STAFF_COCINA))
            self.assertTrue(ec.puede_trasladar(STAFF_COCINA))


class TestMenuCocina(unittest.TestCase):
    def test_menu_cocina_sin_ventas_ni_stock(self) -> None:
        with patch("wa_usabilidad.phone_roles", return_value={"STAFF_COCINA"}), patch(
            "wa_usabilidad.puede_consultar_ventas", return_value=False
        ), patch("wa_usabilidad.puede_consultar_inventario", return_value=False):
            msg = wu.msg_menu_principal(STAFF_COCINA)
            self.assertIn("Trasladar", msg)
            self.assertIn("Producir", msg)
            self.assertNotIn("Ventas", msg)
            self.assertNotIn("stock", msg.lower())

    def test_resolve_menu_cocina_conteo_es_opcion_3(self) -> None:
        with patch("wa_usabilidad.puede_consultar_ventas", return_value=False), patch(
            "wa_usabilidad.puede_consultar_inventario", return_value=False
        ):
            self.assertEqual(wu.resolve_menu_seleccion(STAFF_COCINA, "3"), 4)


if __name__ == "__main__":
    unittest.main()

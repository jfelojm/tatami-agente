"""Tests wa_usabilidad — menú y mensajes por rol."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import wa_usabilidad as wu


class TestMenu(unittest.TestCase):
    def test_comando_menu(self):
        self.assertTrue(wu.es_comando_menu("ayuda"))
        self.assertTrue(wu.es_comando_menu("hola"))
        self.assertFalse(wu.es_comando_menu("2 tortas de chocolate"))

    def test_seleccion_numerica(self):
        self.assertEqual(wu.parse_seleccion_menu("3"), 3)
        self.assertIsNone(wu.parse_seleccion_menu("23"))

    def test_menu_cocina_tres_opciones(self):
        with patch("wa_usabilidad.puede_consultar_ventas", return_value=False), patch(
            "wa_usabilidad.puede_consultar_inventario", return_value=False
        ):
            opts = wu.opciones_menu("593983242667")
            self.assertEqual(len(opts), 3)
            self.assertEqual([o[2] for o in opts], [1, 2, 4])


class TestMensajesPorRol(unittest.TestCase):
    def test_operativo_mensaje_corto(self):
        with patch("wa_usabilidad.phone_roles", return_value={"JEFE_COCINA"}):
            msg = wu.msg_confirmacion_traslado(
                "593992911956",
                cant_txt="2 lotes (1054 gr c/u)",
                etiqueta="torta de chocolate",
                origen="Externa",
                destino="Cocina",
                stock_origen=0,
                unidad_base="gr",
                stock_insuficiente=True,
                periodo_pruebas=True,
                sin_fila_maestro=True,
            )
        self.assertIn("SÍ", msg)
        self.assertIn("modo prueba", msg)
        self.assertNotIn("BD_MP", msg)
        self.assertNotIn("sync", msg.lower())

    def test_admin_ve_detalle_tecnico(self):
        with patch("wa_usabilidad.phone_roles", return_value={"ADMIN", "SOCIO"}):
            msg = wu.msg_confirmacion_traslado(
                "593987122959",
                cant_txt="1 lote (1054 gr)",
                etiqueta="torta de chocolate",
                origen="Externa",
                destino="Cocina",
                stock_origen=0,
                unidad_base="gr",
                stock_insuficiente=True,
                periodo_pruebas=True,
                sin_fila_maestro=True,
            )
        self.assertIn("Sincroniza maestro", msg)


class TestConfirmacionTypo(unittest.TestCase):
    def test_ai_es_si(self):
        self.assertTrue(wu.es_confirmacion_corta("ai"))

    def test_si_normal(self):
        self.assertTrue(wu.es_confirmacion_corta("si"))
        self.assertTrue(wu.es_confirmacion_corta("SÍ"))

    def test_ventas_es_nueva_operacion(self):
        self.assertTrue(wu.parece_nueva_operacion("ventas de hoy"))

    def test_ai_no_es_nueva_operacion(self):
        self.assertFalse(wu.parece_nueva_operacion("ai"))


if __name__ == "__main__":
    unittest.main()

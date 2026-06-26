"""Producción cocina WA: bodega explícita, confirmación y lote sugerido."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import estrategia_config as ec
from whatsapp_webhook import (
    _msg_pedir_bodega_produccion,
    _necesita_pedir_bodega_produccion,
    _parse_bodega_produccion_texto,
)

JEFE_COCINA = "593992911956"
STAFF_COCINA = "593983242667"


class TestBodegaProduccionParse(unittest.TestCase):
    def test_005_suelto(self) -> None:
        self.assertEqual(_parse_bodega_produccion_texto("005"), "BOD-005")

    def test_comando_completo(self) -> None:
        bod = _parse_bodega_produccion_texto(
            "Producir 049 3800 005", cod_sub_ignorar="049"
        )
        self.assertEqual(bod, "BOD-005")


class TestRequiereBodegaCocina(unittest.TestCase):
    def setUp(self) -> None:
        ec._phone_to_roles.cache_clear()

    def test_jefe_cocina_dos_bodegas_pide_explicita(self) -> None:
        with patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {"JEFE_COCINA": "ALLOWLIST_JEFE_COCINA"},
            clear=False,
        ), patch.dict(
            "os.environ",
            {"ALLOWLIST_JEFE_COCINA": JEFE_COCINA},
            clear=False,
        ), patch("config_sheets.cfg", return_value=None):
            ec._phone_to_roles.cache_clear()
            self.assertTrue(ec.requiere_bodega_explicita_produccion(JEFE_COCINA))


class TestConfirmacionNoRepreguntaBodega(unittest.TestCase):
    def test_confirmar_con_bodega_ya_elegida(self) -> None:
        prod = {
            "cods": ["049"],
            "bodega": "BOD-005",
            "confirmar": True,
            "bodega_explicita": True,
        }
        self.assertFalse(_necesita_pedir_bodega_produccion(STAFF_COCINA, prod))

    def test_simulacion_pendiente_sin_explicita_repregunta(self) -> None:
        prod = {
            "cods": ["049"],
            "bodega": "BOD-005",
            "confirmar": True,
        }
        with patch.object(ec, "requiere_bodega_explicita_produccion", return_value=True):
            self.assertTrue(_necesita_pedir_bodega_produccion(STAFF_COCINA, prod))

    def test_simulacion_pendiente_con_explicita_ok(self) -> None:
        prod = {
            "cods": ["049"],
            "bodega": "BOD-005",
            "confirmar": True,
            "bodega_explicita": True,
        }
        with patch.object(ec, "requiere_bodega_explicita_produccion", return_value=True):
            self.assertFalse(_necesita_pedir_bodega_produccion(STAFF_COCINA, prod))


class TestMsgPedirBodegaLote(unittest.TestCase):
    @patch(
        "whatsapp_webhook._rendimiento_sub_display",
        return_value=("22", "uni"),
    )
    @patch(
        "whatsapp_webhook._bodegas_opciones_produccion",
        return_value=["BOD-001", "BOD-005"],
    )
    def test_ejemplo_usa_rendimiento_sub(self, _bod, _rend) -> None:
        msg = _msg_pedir_bodega_produccion(
            STAFF_COCINA,
            {"cods": ["072"], "area": "cocina"},
        )
        self.assertIn("22 uni", msg)
        self.assertIn("PRODUCIR SUB 072 22 uni", msg)
        self.assertNotIn("3800", msg)


if __name__ == "__main__":
    unittest.main()

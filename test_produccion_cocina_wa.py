"""Producción cocina WA: bodega explícita y parseo 005."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import estrategia_config as ec
from whatsapp_webhook import _parse_bodega_produccion_texto

JEFE_COCINA = "593992911956"


class TestBodegaProduccionParse(unittest.TestCase):
    def test_005_suelto(self) -> None:
        self.assertEqual(_parse_bodega_produccion_texto("005"), "BOD-005")

    def test_001_suelto(self) -> None:
        self.assertEqual(_parse_bodega_produccion_texto("001"), "BOD-001")

    def test_no_confunde_sub_049(self) -> None:
        self.assertIsNone(_parse_bodega_produccion_texto("049", cod_sub_ignorar="049"))

    def test_comando_completo(self) -> None:
        bod = _parse_bodega_produccion_texto(
            "Producir 049 3800 005", cod_sub_ignorar="049"
        )
        self.assertEqual(bod, "BOD-005")

    def test_externa_alias(self) -> None:
        self.assertEqual(_parse_bodega_produccion_texto("externa"), "BOD-005")


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
            permitidas = ec.bodegas_permitidas_produccion_sub(JEFE_COCINA)
            self.assertIn("BOD-001", permitidas)
            self.assertIn("BOD-005", permitidas)


if __name__ == "__main__":
    unittest.main()

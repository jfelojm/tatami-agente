"""Parsing de bodega, hilo de conteo y desambiguación 001/002/005 por WhatsApp."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from whatsapp_webhook import (
    _conteo_ctx_touch,
    _es_mensaje_conteo,
    _extraer_codigo_ambiguo_bod_sub,
    _limpiar_ctx_conteo,
    _parse_bodega_conteo,
    _parse_iniciar_conteo_comando,
    _quiere_iniciar_conteo,
)


class TestParseBodegaConteo(unittest.TestCase):
    def test_bod_canonico(self) -> None:
        self.assertEqual(_parse_bodega_conteo("INICIAR CONTEO BOD-005"), "BOD-005")

    def test_iniciar_002_solo(self) -> None:
        self.assertEqual(_parse_bodega_conteo("iniciar 002"), "BOD-002")

    def test_sufijo_suelto_con_seguimiento(self) -> None:
        self.assertEqual(_parse_bodega_conteo("002", seguimiento=True), "BOD-002")
        self.assertIsNone(_parse_bodega_conteo("002", seguimiento=False))

    def test_conteo_externa(self) -> None:
        self.assertEqual(_parse_bodega_conteo("CONTEO EXTERNA"), "BOD-005")

    def test_conteo_cocina(self) -> None:
        self.assertEqual(_parse_bodega_conteo("conteo cocina"), "BOD-001")

    def test_conteo_barra(self) -> None:
        self.assertEqual(_parse_bodega_conteo("Conteo barra"), "BOD-002")


class TestQuiereIniciarConteo(unittest.TestCase):
    def test_conteo_barra_lista_no_inicia(self) -> None:
        bod = _parse_bodega_conteo("Conteo barra")
        self.assertEqual(bod, "BOD-002")
        self.assertFalse(_quiere_iniciar_conteo("Conteo barra", bod, ctx_activo=False))

    def test_iniciar_conteo_barra_si_inicia(self) -> None:
        bod = _parse_bodega_conteo("INICIAR CONTEO barra")
        self.assertTrue(_quiere_iniciar_conteo("INICIAR CONTEO barra", bod, ctx_activo=False))

    def test_iniciar_002_con_ctx(self) -> None:
        bod = _parse_bodega_conteo("iniciar 002", seguimiento=True)
        self.assertTrue(_quiere_iniciar_conteo("iniciar 002", bod, ctx_activo=True))


class TestParseIniciarConteoComando(unittest.TestCase):
    def test_005(self) -> None:
        self.assertEqual(_parse_iniciar_conteo_comando("INICIAR CONTEO 005"), "BOD-005")

    def test_bod_001(self) -> None:
        self.assertEqual(_parse_iniciar_conteo_comando("INICIAR CONTEO BOD-001"), "BOD-001")


class TestHiloConteo(unittest.TestCase):
    def setUp(self) -> None:
        self.wa = "593999999001"
        _limpiar_ctx_conteo(self.wa)

    def tearDown(self) -> None:
        _limpiar_ctx_conteo(self.wa)

    def test_conteo_luego_iniciar_002_es_mensaje_conteo(self) -> None:
        _conteo_ctx_touch(self.wa, active=True)
        self.assertTrue(_es_mensaje_conteo("iniciar 002", self.wa))

    def test_sin_ctx_iniciar_002_no_es_conteo(self) -> None:
        self.assertFalse(_es_mensaje_conteo("iniciar 002", self.wa))

    def test_seguimiento_ctx_activo(self) -> None:
        bod = _parse_bodega_conteo("iniciar 002", seguimiento=True)
        self.assertTrue(
            _quiere_iniciar_conteo("iniciar 002", bod, ctx_activo=True)
        )


class TestAmbiguedadBodSub(unittest.TestCase):
    def test_extrae_iniciar_002(self) -> None:
        self.assertEqual(_extraer_codigo_ambiguo_bod_sub("iniciar 002"), "002")

    def test_no_extrae_iniciar_conteo(self) -> None:
        self.assertIsNone(_extraer_codigo_ambiguo_bod_sub("INICIAR CONTEO 002"))

    def test_no_extrae_producir(self) -> None:
        self.assertIsNone(_extraer_codigo_ambiguo_bod_sub("producir 002"))


class TestSubrecetaActiva(unittest.TestCase):
    @patch("whatsapp_webhook._aliases_subrecetas", return_value=[("salsa miso", "002")])
    @patch("whatsapp_webhook.conectar_sheets", side_effect=RuntimeError("no sheets"))
    def test_002_activo_por_alias(self, _sheets, _alias) -> None:
        from whatsapp_webhook import _subreceta_cod_activo

        self.assertTrue(_subreceta_cod_activo("002"))


if __name__ == "__main__":
    unittest.main()

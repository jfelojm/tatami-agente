"""Parsing de bodega e intención de inicio de conteo por WhatsApp."""

from __future__ import annotations

import unittest

from whatsapp_webhook import (
    _parse_bodega_conteo,
    _parse_iniciar_conteo_comando,
    _quiere_iniciar_conteo,
)


class TestParseBodegaConteo(unittest.TestCase):
    def test_bod_canonico(self) -> None:
        self.assertEqual(_parse_bodega_conteo("INICIAR CONTEO BOD-005"), "BOD-005")

    def test_solo_numero_tras_iniciar(self) -> None:
        self.assertEqual(_parse_bodega_conteo("INICIAR CONTEO 005"), "BOD-005")

    def test_conteo_externa(self) -> None:
        self.assertEqual(_parse_bodega_conteo("CONTEO EXTERNA"), "BOD-005")

    def test_conteo_cocina(self) -> None:
        self.assertEqual(_parse_bodega_conteo("conteo cocina"), "BOD-001")

    def test_conteo_barra(self) -> None:
        self.assertEqual(_parse_bodega_conteo("conteo barra"), "BOD-002")


class TestParseIniciarConteoComando(unittest.TestCase):
    def test_005(self) -> None:
        self.assertEqual(_parse_iniciar_conteo_comando("INICIAR CONTEO 005"), "BOD-005")

    def test_bod_001(self) -> None:
        self.assertEqual(_parse_iniciar_conteo_comando("INICIAR CONTEO BOD-001"), "BOD-001")

    def test_sin_bodega(self) -> None:
        self.assertEqual(_parse_iniciar_conteo_comando("INICIAR CONTEO"), "")

    def test_no_es_comando(self) -> None:
        self.assertIsNone(_parse_iniciar_conteo_comando("CONTEO EXTERNA"))


class TestQuiereIniciarConteo(unittest.TestCase):
    def test_conteo_externa_implicito(self) -> None:
        bod = _parse_bodega_conteo("CONTEO EXTERNA")
        self.assertTrue(_quiere_iniciar_conteo("CONTEO EXTERNA", bod))

    def test_iniciar_explicito(self) -> None:
        self.assertTrue(_quiere_iniciar_conteo("INICIAR CONTEO 005", "BOD-005"))

    def test_revisar_abiertos_no_inicia(self) -> None:
        self.assertFalse(
            _quiere_iniciar_conteo("revisar conteo abiertos externa", "BOD-005")
        )


if __name__ == "__main__":
    unittest.main()

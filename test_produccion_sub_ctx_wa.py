"""Contexto de producción subreceta — nombre suelto tras elegir área."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import whatsapp_webhook as wa


class TestCoincideNombreSubParcial(unittest.TestCase):
    def test_brigadeiro_parcial(self):
        self.assertTrue(
            wa._coincide_nombre_sub("brigadeiro de pistacho", "brigadeiro")
        )

    def test_pan_bao_exacto(self):
        self.assertTrue(wa._coincide_nombre_sub("pan bao", "pan bao"))


class TestConsultaNoProduccion(unittest.TestCase):
    def test_costo_no_es_nombre_sub(self):
        self.assertTrue(wa._es_consulta_no_produccion("costo brigadeiro"))

    def test_nombre_solo_no_es_consulta(self):
        self.assertFalse(wa._es_consulta_no_produccion("brigadeiro"))


class TestResolverProdSubContexto(unittest.TestCase):
    def setUp(self):
        self.wa_id = "593991234567"
        wa._pending_prod_ctx[self.wa_id] = {
            "at": time.monotonic(),
            "area": "cocina",
            "awaiting_sub_name": True,
        }

    def tearDown(self):
        wa._pending_prod_ctx.pop(self.wa_id, None)
        wa._pending_prod_sub.pop(self.wa_id, None)
        wa._pending_prod_area.pop(self.wa_id, None)

    def test_brigadeiro_en_contexto_cocina(self):
        fake = [
            (
                "SUB-049",
                {"nombre_subreceta": "brigadeiro de pistacho", "activa": "SI"},
                [],
            )
        ]
        with patch.object(wa, "_match_sub_codigos_en_texto", return_value=[]):
            with patch.object(wa, "_buscar_subrecetas", return_value=fake):
                prod = wa._resolver_prod_sub("Brigadeiro", self.wa_id)
        self.assertIsNotNone(prod)
        self.assertEqual(prod.get("cods"), ["049"])
        self.assertEqual(prod.get("area"), "cocina")

    def test_costo_brigadeiro_no_produce(self):
        prod = wa._resolver_prod_sub("costo brigadeiro", self.wa_id)
        self.assertIsNone(prod)

    def test_match_alias_brigadeiro_sin_mock(self):
        wa._sub_alias_cache = None
        prod = wa._resolver_prod_sub("Brigadeiro", self.wa_id)
        self.assertIsNotNone(prod)
        self.assertIn("049", prod.get("cods") or [])


class TestParseBatchEsperandoSub(unittest.TestCase):
    def setUp(self):
        self.wa_id = "593991234568"
        wa._pending_prod_ctx[self.wa_id] = {
            "at": time.monotonic(),
            "area": "cocina",
            "awaiting_sub_name": True,
        }

    def tearDown(self):
        wa._pending_prod_ctx.pop(self.wa_id, None)

    def test_brigadeiro_parece_batch(self):
        wa._sub_alias_cache = None
        prod = wa._parse_batch_lenguaje_natural("Brigadeiro", self.wa_id)
        self.assertIsNotNone(prod)
        self.assertEqual(prod.get("cods"), ["049"])


if __name__ == "__main__":
    unittest.main()

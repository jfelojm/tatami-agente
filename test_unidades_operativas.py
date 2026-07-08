"""Tests para interpretación de unidades en traslados y producción."""

from __future__ import annotations

import unittest

from unidades_operativas import (
    parse_cantidad_explicita_base,
    parse_cantidad_presentacion,
    resolver_cantidad_produccion_sub,
    resolver_cantidad_traslado_mp,
)

CAT_MP = {
    "MP-159": {
        "factor_conversion": 750.0,
        "unidad_compra": "botella",
        "unidad_base": "ml",
    },
    "MP-566": {
        "factor_conversion": 750.0,
        "unidad_compra": "botella",
        "unidad_base": "ml",
    },
}

CAT_SUB = {
    "SUB-010": {
        "rendimiento_estandar": 1054.0,
        "unidad": "gr",
        "nombre_subreceta": "Torta de chocolate",
    },
    "SUB-064": {
        "rendimiento_estandar": 16.0,
        "unidad": "uni",
        "nombre_subreceta": "dumpling de camaron",
    },
    "SUB-071": {
        "rendimiento_estandar": 16.0,
        "unidad": "uni",
        "nombre_subreceta": "TARTA VASCA",
    },
}


class TestParsePresentacion(unittest.TestCase):
    def test_una_botella(self):
        self.assertEqual(parse_cantidad_presentacion("transfiere una botella de buchanans"), (1.0, "botella"))

    def test_seis_tortas(self):
        self.assertEqual(parse_cantidad_presentacion("produce 6 tortas de chocolate"), (6.0, "lote"))

    def test_explicito_ml(self):
        self.assertEqual(parse_cantidad_explicita_base("trasladar 750 ml"), 750.0)

    def test_explicito_gr(self):
        self.assertEqual(parse_cantidad_explicita_base("1054 gr de torta"), 1054.0)


class TestResolverTraslado(unittest.TestCase):
    def test_una_botella_a_ml(self):
        r = resolver_cantidad_traslado_mp(
            "159",
            1,
            unidad_base="ml",
            texto="transfiere una botella de buchanans master de consignacion a barra",
            catalogo_mp=CAT_MP,
        )
        self.assertEqual(r["cantidad_base"], 750.0)
        self.assertIn("botella", r["interpretacion"].lower())

    def test_cantidad_explicita_ml(self):
        r = resolver_cantidad_traslado_mp(
            "159",
            1,
            texto="trasladar 750 ml buchanans",
            catalogo_mp=CAT_MP,
        )
        self.assertEqual(r["cantidad_base"], 750.0)

    def test_heuristica_botella(self):
        r = resolver_cantidad_traslado_mp(
            "566",
            2,
            unidad_base="ml",
            catalogo_mp=CAT_MP,
        )
        self.assertEqual(r["cantidad_base"], 1500.0)


class TestResolverProduccion(unittest.TestCase):
    def test_una_torta(self):
        r = resolver_cantidad_produccion_sub(
            "010",
            None,
            texto="produce una torta de chocolate",
            catalogo_sub=CAT_SUB,
        )
        self.assertEqual(r["cantidad_base"], 1054.0)

    def test_seis_tortas(self):
        r = resolver_cantidad_produccion_sub(
            "010",
            None,
            texto="6 tortas de chocolate",
            catalogo_sub=CAT_SUB,
        )
        self.assertEqual(r["cantidad_base"], 6324.0)
        self.assertEqual(r["lotes"], 6.0)

    def test_cantidad_explicita_gr(self):
        r = resolver_cantidad_produccion_sub(
            "010",
            1054,
            texto="producir 1054 gr torta chocolate",
            catalogo_sub=CAT_SUB,
        )
        self.assertEqual(r["cantidad_base"], 1054.0)

    def test_entero_pequeno_como_lotes(self):
        r = resolver_cantidad_produccion_sub(
            "010",
            6,
            catalogo_sub=CAT_SUB,
        )
        self.assertEqual(r["cantidad_base"], 6324.0)

    def test_unidades_plural_no_rompe_singular(self):
        self.assertEqual(parse_cantidad_presentacion("120 unidades"), (120.0, "unidad"))

    def test_dumpling_120_unidades(self):
        r = resolver_cantidad_produccion_sub(
            "064",
            None,
            texto="064, 120 unidades",
            catalogo_sub=CAT_SUB,
        )
        self.assertEqual(r["cantidad_base"], 120.0)
        self.assertEqual(r["lotes"], 7.5)

    def test_dumpling_cmd_120_uni(self):
        r = resolver_cantidad_produccion_sub(
            "064",
            120,
            texto="PRODUCIR SUB 064 120 UNIDADES",
            catalogo_sub=CAT_SUB,
        )
        self.assertEqual(r["cantidad_base"], 120.0)

    def test_tarta_vasca_10_es_unidades_no_lotes(self):
        r = resolver_cantidad_produccion_sub(
            "071",
            10,
            texto="Producir 10 tarta vasca en 005",
            catalogo_sub=CAT_SUB,
        )
        self.assertEqual(r["cantidad_base"], 10.0)
        self.assertAlmostEqual(r["lotes"], 10 / 16.0)


if __name__ == "__main__":
    unittest.main()

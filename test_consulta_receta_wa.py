"""Consultas de receta de plato fuerte — sin LLM ni producción."""

from __future__ import annotations

import unittest

from whatsapp_webhook import (
    _es_consulta_bajo_par,
    _es_consulta_receta_plato,
    _es_pedido_nombres_mp_produccion,
    _extraer_nombre_plato_receta,
    _parse_batch_lenguaje_natural,
    _resolver_prod_sub,
)


class TestConsultaRecetaPlato(unittest.TestCase):
    def test_ingredientes_bibimbap(self):
        t = "Dame los ingredientes de bibimpbap"
        self.assertTrue(_es_consulta_receta_plato(t))
        self.assertEqual(_extraer_nombre_plato_receta(t).lower(), "bibimpbap")
        self.assertFalse(_es_pedido_nombres_mp_produccion(t))
        self.assertIsNone(_parse_batch_lenguaje_natural(t, "59399"))
        self.assertIsNone(_resolver_prod_sub(t, "59399"))

    def test_receta_plato_fuerte_generico(self):
        t = "Quiero saber la receta de un plato fuerte"
        self.assertTrue(_es_consulta_receta_plato(t))
        self.assertEqual(_extraer_nombre_plato_receta(t), "")
        self.assertIsNone(_resolver_prod_sub(t, "59399"))

    def test_producir_sigue_siendo_produccion(self):
        t = "quiero preparar pan bao"
        self.assertFalse(_es_consulta_receta_plato(t))


class TestConsultaBajoPar(unittest.TestCase):
    def test_productos_bajo_par_no_es_batch(self):
        t = "dame los productos bajo par level"
        self.assertTrue(_es_consulta_bajo_par(t))
        self.assertIsNone(_parse_batch_lenguaje_natural(t, "59399"))
        self.assertIsNone(_resolver_prod_sub(t, "59399"))

    def test_top_par(self):
        t = "insumos bajo par top 5"
        self.assertTrue(_es_consulta_bajo_par(t))
        self.assertIsNone(_resolver_prod_sub(t, "59399"))

    def test_producir_batch_sigue_siendo_produccion(self):
        t = "producir batch 051"
        self.assertFalse(_es_consulta_bajo_par(t))
        prod = _parse_batch_lenguaje_natural(t, "59399")
        self.assertIsNotNone(prod)


if __name__ == "__main__":
    unittest.main()

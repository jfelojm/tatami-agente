"""Formato anexo MPs sin stock en alertas órdenes barra."""

from __future__ import annotations

import unittest

from alertas_ordenes_compra_barra import _etiqueta_linea_mp_anexo


class TestEtiquetaMpAnexo(unittest.TestCase):
    def test_prefiere_nombre_maestro_sobre_descripcion(self):
        f = {
            "cod_mp_sistema": "254",
            "nombre_mp": "Pulpa Mora",
            "descripcion_proveedor": "MORA PULPA 100G",
        }
        self.assertEqual(_etiqueta_linea_mp_anexo(f), "• MP 254 — Pulpa Mora")

    def test_no_repite_cod_como_nombre(self):
        f = {
            "cod_mp_sistema": "170",
            "nombre_mp": "170",
            "descripcion_proveedor": "Finestcall Elderflower",
        }
        self.assertEqual(_etiqueta_linea_mp_anexo(f), "• MP 170")

    def test_descripcion_solo_si_falta_nombre(self):
        f = {
            "cod_mp_sistema": "306",
            "nombre_mp": "",
            "descripcion_proveedor": "Naranja deshidratada prov",
        }
        self.assertEqual(_etiqueta_linea_mp_anexo(f), "• MP 306 — Naranja deshidratada prov")


if __name__ == "__main__":
    unittest.main()

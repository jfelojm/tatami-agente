import unittest
from unittest.mock import patch

from dias_cobertura_par import (
    _parse_dias_positivos,
    es_pseudo_mp_subreceta,
    resolver_dias_cobertura_mp,
)


class TestDiasCoberturaPar(unittest.TestCase):
    def test_subreceta_usa_config(self):
        with patch("dias_cobertura_par.dias_cobertura_global_default", return_value=7.0):
            d, fuente = resolver_dias_cobertura_mp("SUB-051")
        self.assertEqual(d, 7.0)
        self.assertEqual(fuente, "subreceta_config")

    def test_mp_sistema_primero(self):
        with patch("dias_cobertura_par._cargar_dias_mp_sistema", return_value={"29": 3.0}):
            with patch("dias_cobertura_par._frecuencia_proveedor_preferido_por_mp", return_value={"29": 7.0}):
                d, fuente = resolver_dias_cobertura_mp("29")
        self.assertEqual(d, 3.0)
        self.assertEqual(fuente, "mp_sistema")

    def test_frecuencia_si_mp_vacio(self):
        with patch("dias_cobertura_par._cargar_dias_mp_sistema", return_value={}):
            with patch("dias_cobertura_par._frecuencia_proveedor_preferido_por_mp", return_value={"92": 14.0}):
                d, fuente = resolver_dias_cobertura_mp("92")
        self.assertEqual(d, 14.0)
        self.assertEqual(fuente, "frecuencia_compra")

    def test_config_sin_datos(self):
        with patch("dias_cobertura_par._cargar_dias_mp_sistema", return_value={}):
            with patch("dias_cobertura_par._frecuencia_proveedor_preferido_por_mp", return_value={}):
                with patch("dias_cobertura_par.dias_cobertura_global_default", return_value=7.0):
                    d, fuente = resolver_dias_cobertura_mp("120")
        self.assertEqual(d, 7.0)
        self.assertEqual(fuente, "config")

    def test_dos_presentaciones_mismo_mp(self):
        """Un solo valor en BD_MP_SISTEMA; no importa cuántas filas en ITEMS_PROV."""
        with patch("dias_cobertura_par._cargar_dias_mp_sistema", return_value={"29": 3.0}):
            with patch("dias_cobertura_par._frecuencia_proveedor_preferido_por_mp", return_value={}):
                d, _ = resolver_dias_cobertura_mp("29")
        self.assertEqual(d, 3.0)

    def test_parse(self):
        self.assertEqual(_parse_dias_positivos("14"), 14.0)
        self.assertIsNone(_parse_dias_positivos(""))
        self.assertTrue(es_pseudo_mp_subreceta("SUB-052"))


if __name__ == "__main__":
    unittest.main()

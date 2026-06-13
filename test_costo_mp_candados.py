"""Tests candados costos MP — licores ml no deben ÷1000."""
import unittest

from costo_mp_canonico import (
    _corregir_cupo_pack,
    _elegir_costo_mp_final,
    validar_cambio_costo_ref,
)


class TestCostoMpCandados(unittest.TestCase):
    def test_corregir_cupo_pack_no_divide_licor_ml(self):
        cu, fixed = _corregir_cupo_pack(0.1309, 750, unidad_base="ml")
        self.assertEqual(cu, 0.1309)
        self.assertFalse(fixed)

    def test_corregir_cupo_pack_divide_kg_gr(self):
        cu, fixed = _corregir_cupo_pack(4.5, 1000, unidad_base="gr")
        self.assertTrue(fixed)
        self.assertAlmostEqual(cu, 0.0045, places=6)

    def test_elegir_costo_prefiere_prov_si_hoja_div1000(self):
        cu, nota = _elegir_costo_mp_final(0.1309, 0.000131)
        self.assertEqual(cu, 0.1309)
        self.assertIn("prov", nota or "")

    def test_validar_bloquea_salto_extremo(self):
        ok, motivo = validar_cambio_costo_ref(0.1309, 0.000131, unidad_base="ml", cu_prov=0.0)
        self.assertFalse(ok)
        self.assertIn("salto", motivo)

    def test_premium_spirit_no_divide(self):
        from numeros_sheets import precio_ref_a_unidad_base

        self.assertAlmostEqual(precio_ref_a_unidad_base(0.5483, 750), 0.5483, places=4)


if __name__ == "__main__":
    unittest.main()

"""Tests unitarios descargo_subreceta (sin red)."""

from __future__ import annotations

import os
import unittest

from descargo_subreceta import (
    calcular_consumo_sub,
    descargo_subrecetas_habilitado,
    preparar_ingredientes_descargo,
    pseudo_mp_cod,
    resolver_costo_unitario_sub,
)


class TestDescargoSubreceta(unittest.TestCase):
    def test_pseudo_mp_cod(self):
        self.assertEqual(pseudo_mp_cod("6"), "SUB-006")
        self.assertEqual(pseudo_mp_cod("051"), "SUB-051")
        self.assertEqual(pseudo_mp_cod("SUB-006"), "SUB-006")

    def test_calcular_consumo(self):
        ing = {"cantidad": "30", "pct_aplicacion": "1", "merma_pct": "0.1"}
        self.assertAlmostEqual(calcular_consumo_sub(ing, 2.0), 66.0)

    def test_preparar_sin_sub(self):
        lineas = [
            {"cod_mp_sistema": "1", "cod_subreceta": ""},
            {"cod_subreceta": "006", "cod_mp_sistema": ""},
        ]
        mp, sub = preparar_ingredientes_descargo(lineas, incluir_sub=False)
        self.assertEqual(len(mp), 1)
        self.assertEqual(len(sub), 0)

    def test_preparar_con_sub(self):
        lineas = [
            {"cod_mp_sistema": "1", "cod_subreceta": ""},
            {"cod_subreceta": "006", "cod_mp_sistema": ""},
        ]
        mp, sub = preparar_ingredientes_descargo(lineas, incluir_sub=True)
        self.assertEqual(len(mp), 1)
        self.assertEqual(len(sub), 1)

    def test_resolver_costo_prioridad_sub(self):
        subs = {
            "6": {
                "costo_unitario_estandar": "0.05",
            }
        }
        self.assertAlmostEqual(
            resolver_costo_unitario_sub("006", subs, {"costo_unitario_ref": "0.99"}),
            0.05,
        )
        self.assertAlmostEqual(
            resolver_costo_unitario_sub("006", {}, {"costo_unitario_ref": "0.12"}),
            0.12,
        )

    def test_flag_env_default_off(self):
        os.environ.pop("DESCARGO_SUBRECETAS", None)
        self.assertFalse(descargo_subrecetas_habilitado())
        os.environ["DESCARGO_SUBRECETAS"] = "1"
        self.assertTrue(descargo_subrecetas_habilitado())
        os.environ.pop("DESCARGO_SUBRECETAS", None)


if __name__ == "__main__":
    unittest.main()

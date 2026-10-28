"""Tests unitarios descargo_subreceta (sin red)."""

from __future__ import annotations

import os
import unittest

from bodegas_config import resolver_bodega_receta
from descargo_subreceta import (
    calcular_consumo_sub,
    descargo_subrecetas_habilitado,
    expandir_subrecetas_directas,
    preparar_ingredientes_descargo,
    pseudo_mp_cod,
    resolver_costo_unitario_sub,
)

os.environ["DESCARGO_SUB_DIRECTO"] = "0"


class TestDescargoSubreceta(unittest.TestCase):
    def test_pseudo_mp_cod(self):
        self.assertEqual(pseudo_mp_cod("6"), "SUB-006")
        self.assertEqual(pseudo_mp_cod("051"), "SUB-051")
        self.assertEqual(pseudo_mp_cod("SUB-006"), "SUB-006")

    def test_calcular_consumo(self):
        ing = {"cantidad": "30", "pct_aplicacion": "1", "merma_pct": "0.1"}
        self.assertAlmostEqual(calcular_consumo_sub(ing, 2.0), 66.0)

    def test_calcular_consumo_coma_decimal(self):
        ing = {"cantidad": "175,67", "pct_aplicacion": "1", "merma_pct": "0"}
        self.assertAlmostEqual(calcular_consumo_sub(ing, 1.0), 175.67)
        ing2 = {"cantidad": "10,00", "pct_aplicacion": "1", "merma_pct": "0"}
        self.assertAlmostEqual(calcular_consumo_sub(ing2, 2.0), 20.0)

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

    def test_expandir_directa_a_mp_e_hija(self):
        directas = {
            "SUB-037": {
                "rend": 600.0,
                "lineas": [
                    {"cod_subreceta_hijo": "SUB-036", "nombre_subreceta_hijo": "kimchi",
                     "cod_mp_sistema": "", "cantidad": "600", "cod_bodega": "BOD-001"},
                    {"cod_mp_sistema": "7", "nombre_mp": "AZUCAR MORENA", "cantidad": "120",
                     "merma_pct": "0", "cod_bodega": "BOD-001", "unidad_base": "gr"},
                ],
            }
        }
        plato = [{"cod_subreceta": "SUB-037", "cantidad": "80", "cod_bodega": "BOD-001",
                  "cod_mp_sistema": ""}]
        mp, sub = expandir_subrecetas_directas(plato, directas)
        self.assertEqual(len(mp), 1)
        self.assertEqual(mp[0]["cod_mp_sistema"], "007")
        self.assertAlmostEqual(mp[0]["cantidad"], 16.0)
        self.assertEqual(mp[0]["via_subreceta"], "SUB-037")
        self.assertEqual(len(sub), 1)
        self.assertEqual(sub[0]["cod_subreceta"], "SUB-036")
        self.assertAlmostEqual(sub[0]["cantidad"], 80.0)

    def test_expandir_sin_directas_no_cambia(self):
        plato = [{"cod_subreceta": "SUB-006", "cantidad": "1", "cod_mp_sistema": ""}]
        mp, sub = expandir_subrecetas_directas(plato, {})
        self.assertEqual(mp, [])
        self.assertEqual(sub, plato)

    def test_bodega_externa_solo_via_subreceta(self):
        self.assertEqual(resolver_bodega_receta({"cod_bodega": "BOD-005"})[1], "BODEGA_NO_DESCARGO")
        self.assertEqual(
            resolver_bodega_receta({"cod_bodega": "BOD-005", "via_subreceta": "SUB-008"}),
            ("BOD-005", None),
        )

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

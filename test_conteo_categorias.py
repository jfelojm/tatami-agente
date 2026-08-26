"""Tests: categorías en conteo (orden + secciones plantilla)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from conteo_fisico import (
    CATEGORIA_SIN,
    normalizar_categoria_conteo,
    ordenar_mps_por_categoria,
)
from plantilla_conteo_sheets import _fila_seccion, _lineas_a_filas


class TestCategoriaConteo(unittest.TestCase):
    def test_normalizar_vacia(self):
        self.assertEqual(normalizar_categoria_conteo(""), CATEGORIA_SIN)
        self.assertEqual(normalizar_categoria_conteo(None), CATEGORIA_SIN)
        self.assertEqual(normalizar_categoria_conteo("  VINO  "), "VINO")

    def test_ordenar_por_categoria_y_nombre(self):
        mps = [
            {"categoria": "VINO/ESPUMANTE", "nombre_mp": "Alto Palermo", "cod_mp_sistema": "302"},
            {"categoria": "Licor/Destilado", "nombre_mp": "Gin", "cod_mp_sistema": "1"},
            {"categoria": "", "nombre_mp": "X", "cod_mp_sistema": "9"},
            {"categoria": "Licor/Destilado", "nombre_mp": "Aperol", "cod_mp_sistema": "2"},
            {"categoria": "subreceta barra", "nombre_mp": "Batch negroni", "cod_mp_sistema": "SUB-1"},
        ]
        out = ordenar_mps_por_categoria(mps)
        cats = [normalizar_categoria_conteo(r.get("categoria")) for r in out]
        # Licor antes que Sin categoría / subreceta / VINO (casefold alpha)
        self.assertEqual(out[0]["nombre_mp"], "Aperol")
        self.assertEqual(out[1]["nombre_mp"], "Gin")
        self.assertIn(CATEGORIA_SIN, cats)
        self.assertEqual(out[-1]["nombre_mp"], "Alto Palermo")

    def test_subreceta_queda_en_su_bloque(self):
        mps = [
            {"categoria": "COCINA", "nombre_mp": "Aceite", "cod_mp_sistema": "1"},
            {"categoria": "subreceta cocina", "nombre_mp": "Aderezo", "cod_mp_sistema": "SUB-1"},
            {"categoria": "COCINA", "nombre_mp": "Sal", "cod_mp_sistema": "2"},
        ]
        out = ordenar_mps_por_categoria(mps)
        self.assertEqual([r["cod_mp_sistema"] for r in out], ["1", "2", "SUB-1"])


class TestPlantillaSecciones(unittest.TestCase):
    def test_lineas_a_filas_inserta_secciones(self):
        lineas = [
            {
                "line_no": 1,
                "cod_mp_sistema": "2",
                "cod_bodega": "BOD-002",
                "categoria": "Licor/Destilado",
                "nombre_mp": "Gin",
                "unidad_base": "ml",
                "stock_sistema_snapshot": 10,
                "conteo_fisico": None,
                "notas": "",
            },
            {
                "line_no": 2,
                "cod_mp_sistema": "302",
                "cod_bodega": "BOD-002",
                "categoria": "VINO/ESPUMANTE",
                "nombre_mp": "Alto Palermo",
                "unidad_base": "ml",
                "stock_sistema_snapshot": 100,
                "conteo_fisico": None,
                "notas": "",
            },
            {
                "line_no": 3,
                "cod_mp_sistema": "1",
                "cod_bodega": "BOD-002",
                "categoria": "Licor/Destilado",
                "nombre_mp": "Aperol",
                "unidad_base": "ml",
                "stock_sistema_snapshot": 5,
                "conteo_fisico": None,
                "notas": "",
            },
        ]
        with patch(
            "plantilla_conteo_sheets.enriquecer_lineas_con_categoria",
            side_effect=lambda xs: sorted(
                xs,
                key=lambda r: (
                    (r.get("categoria") or "").casefold(),
                    (r.get("nombre_mp") or "").casefold(),
                ),
            ),
        ):
            filas = _lineas_a_filas(lineas, con_secciones=True)
        # 2 secciones + 3 MPs
        self.assertEqual(len(filas), 5)
        secs = [f for f in filas if not f[1]]
        mps = [f for f in filas if f[1]]
        self.assertEqual(len(secs), 2)
        self.assertEqual(len(mps), 3)
        self.assertEqual(mps[0][1], "1")  # Aperol
        self.assertEqual(mps[0][3], "Licor/Destilado")
        self.assertEqual(mps[0][4], "Aperol")
        # conteo en col H (index 7)
        self.assertEqual(len(mps[0]), 9)
        self.assertEqual(secs[0], _fila_seccion("Licor/Destilado"))


if __name__ == "__main__":
    unittest.main()

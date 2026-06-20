"""Dashboard ventas: paginación estable y filtro por meses."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from dashboard_routes import (
    _fetch_paginated,
    _parse_meses_query,
    _query_hist_ventas,
    ventas,
)


class TestParseMeses(unittest.TestCase):
    def test_meses_csv(self):
        self.assertEqual(_parse_meses_query(["2026-01,2026-06"]), {"2026-01", "2026-06"})


class TestVentasPaginacion(unittest.TestCase):
    def test_query_usa_order_estable(self):
        captured: list[tuple] = []

        def fake_paginate(q, *, order=()):
            captured.append(order)
            return [
                {
                    "fecha": "2026-01-15",
                    "subtotal": 10.0,
                    "descuento_valor": 0.0,
                    "estado_documento": "ACTIVO",
                    "cod_smart_menu": "1",
                    "variedad_smart_menu": "",
                    "nombre_producto": "TEST",
                    "cod_receta": "001",
                    "cantidad_vendida": 1,
                }
            ]

        sb = MagicMock()
        with patch("dashboard_routes._fetch_paginated", side_effect=fake_paginate):
            rows = _query_hist_ventas(sb, desde="2026-01-01", hasta="2026-01-31")
        self.assertEqual(len(rows), 1)
        self.assertEqual(captured[0], (("fecha", False), ("cod_venta", False)))

    def test_filtro_meses_no_contiguos(self):
        rows_all = [
            {
                "fecha": "2026-01-10",
                "subtotal": 100.0,
                "descuento_valor": 0.0,
                "estado_documento": "ACTIVO",
                "cod_smart_menu": "1",
                "variedad_smart_menu": "",
                "nombre_producto": "A",
                "cod_receta": "001",
                "cantidad_vendida": 1,
            },
            {
                "fecha": "2026-02-10",
                "subtotal": 50.0,
                "descuento_valor": 0.0,
                "estado_documento": "ACTIVO",
                "cod_smart_menu": "2",
                "variedad_smart_menu": "",
                "nombre_producto": "B",
                "cod_receta": "002",
                "cantidad_vendida": 1,
            },
            {
                "fecha": "2026-06-10",
                "subtotal": 200.0,
                "descuento_valor": 0.0,
                "estado_documento": "ACTIVO",
                "cod_smart_menu": "3",
                "variedad_smart_menu": "",
                "nombre_producto": "C",
                "cod_receta": "003",
                "cantidad_vendida": 1,
            },
        ]
        sb = MagicMock()
        with patch("dashboard_routes._fetch_paginated", return_value=rows_all):
            out = _query_hist_ventas(
                sb,
                desde="2026-01-01",
                hasta="2026-06-30",
                meses={"2026-01", "2026-06"},
            )
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(
            sum(float(r["subtotal"]) for r in out),
            300.0,
        )


@unittest.skipUnless(os.getenv("SUPABASE_URL"), "requiere Supabase")
class TestVentasIntegracion(unittest.TestCase):
    def test_anio_coincide_suma_meses(self):
        token = os.getenv("DASHBOARD_TOKEN", "tatami2026")
        year = ventas(
            token=token,
            desde="2026-01-01",
            hasta="2026-12-31",
            agrup="mes",
            punto_venta=None,
            categoria=None,
            plato=None,
            mes=None,
            dia_semana=None,
            incluir_socios=False,
            orden="desc",
        )
        tot = round(
            year["total_barra"] + year["total_cocina"] + year["total_otro"],
            2,
        )
        chart = round(
            sum(
                year["barra"][i] + year["cocina"][i] + year["otro"][i]
                for i in range(len(year["labels"]))
            ),
            2,
        )
        self.assertAlmostEqual(tot, chart, places=1)

        jun = ventas(
            token=token,
            desde="2026-06-01",
            hasta="2026-06-30",
            agrup="mes",
            punto_venta=None,
            categoria=None,
            plato=None,
            mes=None,
            dia_semana=None,
            incluir_socios=False,
            orden="desc",
        )
        jun_tot = round(
            jun["total_barra"] + jun["total_cocina"] + jun["total_otro"],
            2,
        )
        if "2026-06" in year["labels"]:
            i = year["labels"].index("2026-06")
            jun_bar = round(
                year["barra"][i] + year["cocina"][i] + year["otro"][i],
                2,
            )
            self.assertEqual(jun_tot, jun_bar)


if __name__ == "__main__":
    unittest.main()

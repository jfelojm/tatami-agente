"""Tests dashboard_services.periodos."""

import unittest
from datetime import date

from dashboard_services.periodos import (
    acumulado_anio,
    delta_pct,
    mismo_periodo_anio_anterior,
    periodo_anterior,
)


class TestPeriodos(unittest.TestCase):
    def test_periodo_anterior_mismo_largo(self):
        ini, fin = periodo_anterior(date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(ini, date(2026, 5, 1))
        self.assertEqual(fin, date(2026, 5, 31))

    def test_mismo_periodo_anio_anterior(self):
        ini, fin = mismo_periodo_anio_anterior(date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(ini, date(2025, 6, 1))
        self.assertEqual(fin, date(2025, 6, 30))

    def test_acumulado_anio(self):
        ini, fin = acumulado_anio(date(2026, 6, 15))
        self.assertEqual(ini, date(2026, 1, 1))
        self.assertEqual(fin, date(2026, 6, 15))

    def test_delta_pct(self):
        self.assertEqual(delta_pct(110, 100), 10.0)
        self.assertIsNone(delta_pct(0, 0))


if __name__ == "__main__":
    unittest.main()

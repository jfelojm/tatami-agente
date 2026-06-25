"""Tests optimización compras: filtro en memoria y cache."""

import unittest
from datetime import date

from dashboard_services.compras import _filtrar_mov_por_rango, build_resumen_compras
from dashboard_services.dashboard_cache import get, make_key, set


class TestComprasFiltro(unittest.TestCase):
    def test_filtrar_mov_por_rango(self) -> None:
        rows = [
            {"fecha": "2026-05-15", "costo_total": 10},
            {"fecha": "2026-06-01T12:00:00", "costo_total": 20},
            {"fecha": "2026-07-01", "costo_total": 30},
        ]
        out = _filtrar_mov_por_rango(rows, date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["costo_total"], 20)


class TestComprasSingleQuery(unittest.TestCase):
    def test_una_sola_llamada_query_mov(self) -> None:
        calls: list[tuple[str, str]] = []

        def query_mov(d: str, h: str) -> list[dict]:
            calls.append((d, h))
            return [{"fecha": d, "tipo_mov": "ENTRADA", "costo_total": 0}]

        def query_fac(d: str, h: str) -> list[dict]:
            return []

        build_resumen_compras(
            query_mov_fn=query_mov,
            query_facturas_fn=query_fac,
            rows_mp=[],
            rows_prov=[],
            desde=date(2026, 6, 1),
            hasta=date(2026, 6, 30),
            agrup="mes",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "2025-06-01")
        self.assertEqual(calls[0][1], "2026-06-30")


class TestDashboardCache(unittest.TestCase):
    def test_cache_roundtrip(self) -> None:
        key = make_key("t", a=1, b="x")
        set(key, {"ok": True})
        self.assertEqual(get(key), {"ok": True})
        self.assertIsNone(get("missing"))


if __name__ == "__main__":
    unittest.main()

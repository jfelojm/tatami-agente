"""Tests optimización compras: filtro en memoria y cache."""

import unittest
from datetime import date

from dashboard_services.compras import (
    _filtrar_mov_por_rango,
    build_resumen_compras,
    contexto_filtros_compras,
    filtrar_compras_inventario,
    total_compras_dashboard,
)
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


class TestFiltroComprasCompartido(unittest.TestCase):
    def test_wa_y_dashboard_mismo_total(self) -> None:
        rows_mp = [
            {"cod_mp_sistema": "065", "cod_bodega": "BOD-001", "nombre_mp": "Panceta"},
        ]
        rows_prov = [
            {
                "razon_social": "PACHECO",
                "ruc": "0101298701001",
                "Tipo": "Cocina",
            }
        ]
        facturas = [
            {
                "num_factura": "F-1",
                "ruc_proveedor": "0101298701001",
                "razon_social": "PACHECO",
            }
        ]
        rows = [
            {
                "fecha": "2026-06-10",
                "tipo_mov": "ENTRADA",
                "cod_mp_sistema": "065",
                "nombre_mp": "Panceta",
                "cantidad_mov": 1000,
                "costo_total": 50.0,
                "num_documento": "F-1",
                "cod_bodega_destino": "BOD-001",
                "observaciones": "PANCETA | RUC:0101298701001",
            },
            {
                "fecha": "2026-06-10",
                "tipo_mov": "ENTRADA_COSTO_HIST",
                "cod_mp_sistema": "065",
                "nombre_mp": "Panceta",
                "cantidad_mov": 0,
                "costo_total": 12.5,
                "num_documento": "F-1",
                "cod_bodega_destino": "BOD-001",
                "observaciones": "PANCETA | RUC:0101298701001",
            },
            {
                "fecha": "2026-06-10",
                "tipo_mov": "ENTRADA",
                "cod_mp_sistema": "000",
                "nombre_mp": "Sin clasificar",
                "cantidad_mov": 1,
                "costo_total": 99.0,
                "num_documento": "F-1",
                "cod_bodega_destino": "BOD-001",
                "observaciones": "SIN_CATALOGO",
            },
            {
                "fecha": "2026-06-10",
                "tipo_mov": "ENTRADA",
                "cod_mp_sistema": "065",
                "nombre_mp": "Panceta",
                "cantidad_mov": 100,
                "costo_total": 5.0,
                "num_documento": "F-1",
                "cod_bodega_destino": "BOD-099",
                "observaciones": "otra bodega",
            },
        ]
        dash = total_compras_dashboard(rows, facturas, rows_mp, rows_prov)
        ctx = contexto_filtros_compras(rows_mp, rows_prov, facturas)
        wa = filtrar_compras_inventario(
            rows,
            mps_validos=ctx["mps_validos"],
            prov_inv=ctx["prov_inv"],
            fact_por_num=ctx["fact_por_num"],
            exigir_proveedor=True,
        )
        wa_total = round(sum(float(r["costo_total"]) for r in wa), 2)
        self.assertEqual(dash, 62.5)
        self.assertEqual(wa_total, dash)

    def test_ruc_sin_cero_inicial_y_duplicado(self) -> None:
        """Colemun-like: RUC 12 dígitos + fila duplicada sin razón social."""
        rows_mp = [
            {"cod_mp_sistema": "231", "cod_bodega": "BOD-002", "nombre_mp": "Codorniu"},
        ]
        rows_prov = [
            {
                "razon_social": "COLEMUN S.A.",
                "ruc": "0992613092001",
                "Tipo": "Barra",
            }
        ]
        # Duplicado: primero RUC corto (sin 0), luego RUC completo
        facturas = [
            {
                "num_factura": "019-001-000054369",
                "ruc_proveedor": "992613092001",
                "estado": "COMPLETA",
                "razon_social": "",
            },
            {
                "num_factura": "019-001-000054369",
                "ruc_proveedor": "0992613092001",
                "estado": "POR_RECIBIR",
                "razon_social": "",
            },
        ]
        rows = [
            {
                "fecha": "2026-07-15",
                "tipo_mov": "ENTRADA",
                "cod_mp_sistema": "231",
                "nombre_mp": "Codorniu",
                "cantidad_mov": 2,
                "costo_total": 308.0,
                "num_documento": "019-001-000054369",
                "cod_bodega_destino": "BOD-002",
                "observaciones": "ITEM",
            },
        ]
        dash = total_compras_dashboard(rows, facturas, rows_mp, rows_prov)
        ctx = contexto_filtros_compras(rows_mp, rows_prov, facturas)
        wa = filtrar_compras_inventario(
            rows,
            mps_validos=ctx["mps_validos"],
            prov_inv=ctx["prov_inv"],
            fact_por_num=ctx["fact_por_num"],
            exigir_proveedor=True,
        )
        self.assertEqual(dash, 308.0)
        self.assertEqual(round(sum(float(r["costo_total"]) for r in wa), 2), dash)
        # Solo el RUC corto (como a veces llega por WA) también debe resolver
        facturas_solo_corto = [
            {
                "num_factura": "019-001-000054369",
                "ruc_proveedor": "992613092001",
                "estado": "COMPLETA",
                "razon_social": "",
            }
        ]
        dash2 = total_compras_dashboard(rows, facturas_solo_corto, rows_mp, rows_prov)
        self.assertEqual(dash2, 308.0)


if __name__ == "__main__":
    unittest.main()

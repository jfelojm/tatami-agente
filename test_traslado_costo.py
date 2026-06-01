"""Regresión: costo en traslados para todos los pares permitidos."""

from __future__ import annotations

import unittest

from bodegas_config import _TRASLADOS_DIRIGIDOS, traslado_permitido
from inventario_traslado import (
    construir_par_movimientos_traslado,
    costo_ref_desde_filas_maestro,
    validar_mov_traslado_lleva_costo,
)
from recalcular_stock_sheets import (
    TIPOS_SUMA_DESTINO,
    _BODEGA_PRIO_COSTO_HERMANO,
    _build_costo_ref_por_mp_desde_hoja,
)


class TestTrasladoCosto(unittest.TestCase):
    def test_todos_los_pares_matriz_son_permitidos(self):
        for origen, destino in _TRASLADOS_DIRIGIDOS:
            self.assertTrue(
                traslado_permitido(origen, destino),
                f"{origen}→{destino} debe ser traslado permitido",
            )

    def test_movimientos_incluyen_costo_desde_origen(self):
        sal, ent = construir_par_movimientos_traslado(
            cod_mp="566",
            nombre_mp="Buchanan",
            bodega_origen="BOD-002",
            bodega_destino="BOD-003",
            cantidad=750.0,
            unidad_base="ml",
            costo_unitario_ref=0.189,
            cod_base="TRA-TEST",
            fecha_iso="2026-05-28T12:00:00",
            registrado_por="TEST",
        )
        for m in (sal, ent):
            self.assertEqual(m["costo_unitario"], 0.189)
            self.assertAlmostEqual(m["costo_total"], 0.189 * 750.0, places=4)
            self.assertEqual(m["origen_documento"], "TRASLADO")
        self.assertEqual(sal["tipo_mov"], "TRASLADO_SALIDA")
        self.assertEqual(ent["tipo_mov"], "TRASLADO_ENTRADA")
        self.assertEqual(sal["cod_bodega_origen"], "BOD-002")
        self.assertEqual(ent["cod_bodega_destino"], "BOD-003")

    def test_mov_sin_costo_origen_no_inventa_costo(self):
        sal, ent = construir_par_movimientos_traslado(
            cod_mp="300",
            nombre_mp="Fiorente",
            bodega_origen="BOD-002",
            bodega_destino="BOD-003",
            cantidad=100.0,
            unidad_base="ml",
            costo_unitario_ref=0.0,
            cod_base="TRA-TEST2",
            fecha_iso="2026-05-28T12:00:00",
            registrado_por="TEST",
        )
        self.assertNotIn("costo_unitario", sal)
        self.assertNotIn("costo_unitario", ent)

    def test_costo_ref_desde_filas_maestro(self):
        rows = [
            {"cod_mp_sistema": "566", "cod_bodega": "BOD-002", "costo_unitario_ref": 0.189},
            {"cod_mp_sistema": "566", "cod_bodega": "BOD-003", "costo_unitario_ref": 0},
        ]
        self.assertAlmostEqual(costo_ref_desde_filas_maestro(rows, "566", "BOD-002"), 0.189)
        self.assertEqual(costo_ref_desde_filas_maestro(rows, "566", "BOD-005"), 0.0)

    def test_traslado_entrada_cuenta_en_tipos_suma(self):
        self.assertIn("TRASLADO_ENTRADA", TIPOS_SUMA_DESTINO)

    def test_hereda_costo_prioridad_bodega(self):
        data = [
            ["566", "BOD-003", "0"],
            ["566", "BOD-002", "0.189"],
            ["566", "BOD-001", "0.05"],
        ]
        mp = _build_costo_ref_por_mp_desde_hoja(
            data, col_cod=0, col_bod=1, col_costo=2
        )
        self.assertAlmostEqual(mp["566"], 0.05)

    def test_validar_mov_legacy_sin_costo(self):
        self.assertFalse(
            validar_mov_traslado_lleva_costo({"tipo_mov": "TRASLADO_ENTRADA", "costo_unitario": 0})
        )
        self.assertTrue(
            validar_mov_traslado_lleva_costo({"tipo_mov": "TRASLADO_ENTRADA", "costo_unitario": 0.1})
        )


if __name__ == "__main__":
    unittest.main()

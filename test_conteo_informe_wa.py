"""Informe WA de conteo: completo, saldo neto y partición."""

from __future__ import annotations

import unittest

from conteo_routes import (
    _WA_CONTEO_MAX,
    _formatear_informe_wa_partes,
    _resumen_saldo_deltas,
)


def _delta(nombre: str, delta: float, pct: float, valor: float | None, uni: str = "ml"):
    return {
        "cod_mp_sistema": "1",
        "nombre_mp": nombre,
        "unidad_base": uni,
        "stock_snapshot": 100.0,
        "conteo_fisico": 100.0 + delta,
        "delta": delta,
        "delta_pct": pct,
        "valor_delta": valor,
        "costo_ref": 0.01,
    }


class TestInformeConteoWa(unittest.TestCase):
    def test_incluye_todos_y_saldo(self) -> None:
        deltas = [
            _delta(f"Item {i}", 10.0 if i % 2 == 0 else -5.0, 10.0, 2.5 if i % 2 == 0 else -1.0)
            for i in range(20)
        ]
        ciclo = {"cod_bodega": "BOD-002", "semana_iso": 33, "anio": 2026}
        partes = _formatear_informe_wa_partes(ciclo, deltas, "test")
        texto = "\n".join(partes)
        self.assertIn("Diferencias ≥1%: 20", texto)
        self.assertIn("Saldo $:", texto)
        self.assertIn("neto $", texto)
        self.assertNotIn("ítems más", texto)
        for i in range(20):
            self.assertIn(f"Item {i}", texto)
        for p in partes:
            self.assertLessEqual(len(p), _WA_CONTEO_MAX + 20)

    def test_saldo_neto(self) -> None:
        deltas = [
            _delta("A", 10, 5, 12.0),
            _delta("B", -3, -2, -4.0),
            _delta("C", 1, 1, None),
        ]
        s = _resumen_saldo_deltas(deltas)
        self.assertEqual(s["valor_positivo"], 12.0)
        self.assertEqual(s["valor_negativo"], -4.0)
        self.assertEqual(s["saldo_neto"], 8.0)
        self.assertEqual(s["sin_costo"], 1)

    def test_pct_sin_doble_signo(self) -> None:
        deltas = [_delta("X", -50.0, -50.0, -1.0)]
        partes = _formatear_informe_wa_partes(
            {"cod_bodega": "BOD-002", "semana_iso": 1, "anio": 2026},
            deltas,
            "t",
        )
        self.assertIn("(-50.0%)", partes[0])
        self.assertNotIn("+-50", partes[0])


if __name__ == "__main__":
    unittest.main()

"""Series mensuales de costo real y margen."""
import unittest

from dashboard_services.rentabilidad import evolucion_costo_margen_mes


class EvolucionCostoMargenMes(unittest.TestCase):
    def test_ordena_y_calcula_margen(self):
        out = evolucion_costo_margen_mes(
            {
                "2026-02": {"vta": 200, "costo": 80},
                "2025-12": {"vta": 100, "costo": 40},
            }
        )
        self.assertEqual(out["labels_mes"], ["2025-12", "2026-02"])
        self.assertEqual(out["vta_mes"], [100.0, 200.0])
        self.assertEqual(out["costo_mes"], [40.0, 80.0])
        self.assertEqual(out["margen_mes"], [60.0, 120.0])
        self.assertEqual(out["margen_mes_pct"], [60.0, 60.0])

    def test_mes_sin_venta(self):
        out = evolucion_costo_margen_mes({"": {"vta": 10, "costo": 1}, "2026-01": {"vta": 0, "costo": 5}})
        self.assertEqual(out["labels_mes"], ["2026-01"])
        self.assertEqual(out["margen_mes"], [-5.0])
        self.assertEqual(out["margen_mes_pct"], [0.0])


if __name__ == "__main__":
    unittest.main()

"""Stock en inventario vivo: claves MP con ceros a la izquierda."""

from __future__ import annotations

import unittest

from dashboard_services.inventario_vivo import build_inventario_vivo
from recalcular_stock_sheets import _clave_stock, build_stock_calculado


class TestInventarioVivoStock(unittest.TestCase):
    def test_clave_stock_cero_izquierda(self):
        self.assertEqual(_clave_stock("089", "BOD-005")[0], "089")
        self.assertEqual(_clave_stock("89", "BOD-005")[0], "089")

    def test_dashboard_usa_stock_map_con_mp_089(self):
        movs = [
            {
                "cod_mp_sistema": "089",
                "tipo_mov": "AJUSTE_POSITIVO",
                "cantidad_mov": 3186.9565,
                "cod_bodega_origen": "",
                "cod_bodega_destino": "BOD-005",
            }
        ]
        stock = build_stock_calculado(movs)
        rows = [
            {
                "cod_mp_sistema": "089",
                "nombre_mp": "PASTA DE PISTACHO",
                "categoria": "COCINA",
                "unidad_base": "gr",
                "cod_bodega": "BOD-005",
                "par_level": "7033",
                "consumo_diario_calculado": "78",
                "costo_unitario_ref": "0.03",
            }
        ]
        out = build_inventario_vivo(rows, stock_map=stock, cod_bodega="BOD-005")
        pistacho = [it for it in out["items"] if it["cod_mp_sistema"] == "089"]
        self.assertEqual(len(pistacho), 1)
        self.assertAlmostEqual(pistacho[0]["stock"], 3186.9565, places=2)


if __name__ == "__main__":
    unittest.main()

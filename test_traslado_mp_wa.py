"""Traslados WA: nombre primero, desambiguación, sin inventar códigos."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bodegas_config import resolver_cod_bodega, traslado_permitido


FILAS_BUCHANAN = [
    {
        "cod_mp_sistema": "566",
        "nombre_mp": "Whisky Buchanans 18",
        "cod_bodega": "BOD-003",
        "stock_actual": "4500",
        "unidad_base": "ml",
    },
    {
        "cod_mp_sistema": "566",
        "nombre_mp": "Whisky Buchanans 18",
        "cod_bodega": "BOD-002",
        "stock_actual": "0",
        "unidad_base": "ml",
    },
    {
        "cod_mp_sistema": "159",
        "nombre_mp": "Whisky Buchanans Master",
        "cod_bodega": "BOD-003",
        "stock_actual": "3000",
        "unidad_base": "ml",
    },
    {
        "cod_mp_sistema": "158",
        "nombre_mp": "Whisky Buchanans",
        "cod_bodega": "BOD-002",
        "stock_actual": "100",
        "unidad_base": "ml",
    },
]


class TestResolverBodega(unittest.TestCase):
    def test_alias_consignacion(self):
        self.assertEqual(resolver_cod_bodega("consignacion"), "BOD-003")

    def test_traslado_consignacion_barra(self):
        self.assertTrue(traslado_permitido("consignacion", "barra"))


class TestBusquedaMp(unittest.TestCase):
    def test_tokens_buchanan_18(self):
        from whatsapp_webhook import _buscar_mp_por_nombre_o_codigo

        with patch("whatsapp_webhook.leer_bd_mp_sistema", return_value=FILAS_BUCHANAN):
            hits = _buscar_mp_por_nombre_o_codigo("buchanan 18")
        cods = {h["cod_mp_sistema"] for h in hits}
        self.assertEqual(cods, {"566"})


class TestResolverMpPorNombre(unittest.TestCase):
    def test_buchanan_18_resuelve_solo(self):
        from whatsapp_webhook import _resolver_mp_por_nombre

        with patch("whatsapp_webhook.leer_bd_mp_sistema", return_value=FILAS_BUCHANAN):
            r = _resolver_mp_por_nombre(
                FILAS_BUCHANAN,
                nombre_mp="buchanan 18",
                bodega_origen="consignacion",
            )
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["cod_mp"], "566")

    def test_ignora_codigo_inventado_si_hay_nombre(self):
        from whatsapp_webhook import _resolver_mp_por_nombre

        with patch("whatsapp_webhook.leer_bd_mp_sistema", return_value=FILAS_BUCHANAN):
            r = _resolver_mp_por_nombre(
                FILAS_BUCHANAN,
                nombre_mp="buchanan 18",
                cod_mp="MP-1224",
                bodega_origen="consignacion",
            )
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["cod_mp"], "566")

    def test_buchanan_ambiguo_pide_eleccion(self):
        from whatsapp_webhook import _resolver_mp_por_nombre

        with patch("whatsapp_webhook.leer_bd_mp_sistema", return_value=FILAS_BUCHANAN):
            r = _resolver_mp_por_nombre(
                FILAS_BUCHANAN,
                nombre_mp="buchanan",
                bodega_origen="consignacion",
            )
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("requiere_eleccion"))
        self.assertGreaterEqual(len(r.get("opciones", [])), 2)

    def test_codigo_inventado_sin_nombre_error(self):
        from whatsapp_webhook import _resolver_mp_por_nombre

        r = _resolver_mp_por_nombre([], nombre_mp="", cod_mp="1224")
        self.assertFalse(r.get("ok"))
        self.assertIn("error", r)


class TestTrasladarPorNombre(unittest.TestCase):
    def test_traslado_solo_nombre(self):
        from whatsapp_webhook import tool_trasladar_mp

        with patch("whatsapp_webhook.leer_bd_mp_sistema", return_value=FILAS_BUCHANAN):
            r = tool_trasladar_mp(
                {
                    "nombre_mp": "buchanan 18",
                    "bodega_origen": "consignacion",
                    "bodega_destino": "barra",
                    "cantidad": 750,
                    "confirmado": False,
                }
            )
        self.assertTrue(r.get("requiere_confirmacion"))
        self.assertIn("Buchanans 18", r.get("mensaje", ""))
        self.assertNotIn("1224", r.get("mensaje", ""))


if __name__ == "__main__":
    unittest.main()

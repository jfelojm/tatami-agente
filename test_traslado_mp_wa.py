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


class TestTrasladoVsProduccion(unittest.TestCase):
    def test_traslado_no_es_produccion(self):
        from whatsapp_webhook import _es_mensaje_traslado, _resolver_prod_sub

        texto = "traslada una mp de 005 a 001"
        self.assertTrue(_es_mensaje_traslado(texto))
        with patch("whatsapp_webhook._prod_ctx_get", return_value={}):
            self.assertIsNone(_resolver_prod_sub(texto, "593999999999"))

    def test_bodegas_005_001(self):
        from whatsapp_webhook import _parse_traslado_bodegas

        r = _parse_traslado_bodegas("traslada una mp de 005 a 001")
        self.assertIsNotNone(r)
        self.assertEqual(r["bodega_origen"], "BOD-005")
        self.assertEqual(r["bodega_destino"], "BOD-001")

    def test_match_sub_no_confunde_bodegas(self):
        from whatsapp_webhook import _match_sub_codigos_en_texto

        cods = _match_sub_codigos_en_texto("traslada una mp de 005 a 001")
        self.assertEqual(cods, [])

    def test_traslada_producto_sin_nombre(self):
        from whatsapp_webhook import _extraer_nombre_mp_traslado

        self.assertEqual(_extraer_nombre_mp_traslado("traslada producto"), "")
        self.assertEqual(_extraer_nombre_mp_traslado("traslada papa de 005 a 001"), "papa")

    def test_traslada_producto_no_es_produccion(self):
        from whatsapp_webhook import _es_mensaje_traslado, _resolver_prod_sub

        self.assertTrue(_es_mensaje_traslado("traslada producto"))
        with patch("whatsapp_webhook._prod_ctx_get", return_value={"last_cods": ["005", "001"]}):
            self.assertIsNone(_resolver_prod_sub("traslada producto", "593999999999"))


    def test_raslada_typo_es_traslado(self):
        from whatsapp_webhook import (
            _es_mensaje_traslado,
            _normalizar_texto_comando_wa,
        )

        self.assertEqual(_normalizar_texto_comando_wa("raslada producto"), "traslada producto")
        self.assertTrue(_es_mensaje_traslado("raslada producto"))


    def test_trasladar_producto_no_batch_con_ctx_cocina(self):
        from whatsapp_webhook import (
            _es_intento_produccion,
            _es_traslado_generico_sin_detalle,
            _parse_batch_lenguaje_natural,
            _resolver_prod_sub,
        )

        with patch("whatsapp_webhook._prod_ctx_get", return_value={"area": "cocina"}):
            self.assertFalse(_es_intento_produccion("trasladar producto"))
            self.assertIsNone(_parse_batch_lenguaje_natural("trasladar producto", "59399"))
            self.assertIsNone(_resolver_prod_sub("trasladar producto", "59399"))
        self.assertTrue(_es_traslado_generico_sin_detalle("trasladar producto"))
        self.assertTrue(_es_traslado_generico_sin_detalle("trasladar materia prima"))

    def test_traslado_con_unicode_invisible(self):
        from whatsapp_webhook import _es_mensaje_traslado, _normalizar_texto_comando_wa

        raw = "traslad\u200bar producto"
        self.assertIn("trasladar", _normalizar_texto_comando_wa(raw))
        self.assertTrue(_es_mensaje_traslado(raw))


    def test_es_subreceta_no_es_produccion(self):
        from whatsapp_webhook import _es_intento_produccion, _es_aclaracion_traslado_sub

        self.assertFalse(_es_intento_produccion("es una subreceta"))
        self.assertTrue(_es_aclaracion_traslado_sub("es una subreceta"))

    def test_resolver_sub_torta_cantidad(self):
        from whatsapp_webhook import _resolver_subreceta_para_traslado

        with patch(
            "whatsapp_webhook._match_sub_codigos_en_texto",
            return_value=["010"],
        ):
            with patch(
                "whatsapp_webhook.conectar_sheets",
                side_effect=RuntimeError("offline"),
            ):
                with patch(
                    "unidades_operativas.cargar_rendimiento_subrecetas",
                    return_value={
                        "SUB-010": {
                            "rendimiento_estandar": 1054.0,
                            "unidad": "gr",
                            "nombre_subreceta": "Torta de chocolate",
                        }
                    },
                ):
                    r = _resolver_subreceta_para_traslado(
                        "trasladar 5 tortas de chocolate de cocina a externa"
                    )
        self.assertIsNotNone(r)
        self.assertEqual(r["cod_sub"], "010")
        self.assertEqual(r["cantidad"], 5270.0)


if __name__ == "__main__":
    unittest.main()

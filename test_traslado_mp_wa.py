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

    def test_sub_061_sin_fila_maestro(self):
        from whatsapp_webhook import _resolver_mp_por_nombre

        with patch(
            "whatsapp_webhook._match_sub_codigos_en_texto",
            return_value=["061"],
        ):
            with patch(
                "whatsapp_webhook.conectar_sheets",
                side_effect=RuntimeError("offline"),
            ):
                with patch(
                    "unidades_operativas.cargar_rendimiento_subrecetas",
                    return_value={
                        "SUB-061": {
                            "rendimiento_estandar": 1054.0,
                            "unidad": "gr",
                            "nombre_subreceta": "torta de chocolate",
                        }
                    },
                ):
                    r = _resolver_mp_por_nombre(
                        [],
                        nombre_mp="torta de chocolate",
                        bodega_origen="BOD-005",
                    )
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["cod_mp"], "SUB-061")
        self.assertTrue(r.get("es_subreceta"))


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
        self.assertTrue(_es_traslado_generico_sin_detalle("trasladar"))

    def test_traslado_implicito_extrae_nombre(self):
        from whatsapp_webhook import _extraer_nombre_mp_traslado

        self.assertEqual(
            _extraer_nombre_mp_traslado("torta de chocolate de 005 a 001"),
            "torta de chocolate",
        )

    def test_traslado_sub_sin_maestro_simula(self):
        from whatsapp_webhook import tool_trasladar_mp

        with patch("whatsapp_webhook.leer_bd_mp_sistema", return_value=[]):
            with patch(
                "whatsapp_webhook._resolver_mp_por_nombre",
                return_value={
                    "ok": True,
                    "cod_mp": "SUB-061",
                    "nombre_mp": "torta de chocolate",
                    "es_subreceta": True,
                    "unidad_base": "gr",
                },
            ):
                with patch(
                    "unidades_operativas.resolver_cantidad_traslado_mp",
                    return_value={
                        "cantidad_base": 1054.0,
                        "interpretacion": "1 lote (1054 gr)",
                    },
                ):
                    r = tool_trasladar_mp(
                        {
                            "cod_mp_sistema": "SUB-061",
                            "nombre_mp": "torta de chocolate",
                            "bodega_origen": "BOD-005",
                            "bodega_destino": "BOD-001",
                            "cantidad": 1,
                            "confirmado": False,
                            "ignorar_stock": True,
                            "_wa_id": "593987122959",
                        }
                    )
        self.assertTrue(r.get("requiere_confirmacion"))
        self.assertIn("torta de chocolate", r.get("mensaje", ""))
        self.assertIn("SÍ", r.get("mensaje", ""))
        self.assertNotIn("sin conversión", r.get("mensaje", "").lower())

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
            return_value=["061"],
        ):
            with patch(
                "whatsapp_webhook.conectar_sheets",
                side_effect=RuntimeError("offline"),
            ):
                with patch(
                    "unidades_operativas.cargar_rendimiento_subrecetas",
                    return_value={
                        "SUB-061": {
                            "rendimiento_estandar": 1054.0,
                            "unidad": "gr",
                            "nombre_subreceta": "torta de chocolate",
                        }
                    },
                ):
                    r = _resolver_subreceta_para_traslado(
                        "trasladar 5 tortas de chocolate de cocina a externa"
                    )
        self.assertIsNotNone(r)
        self.assertEqual(r["cod_sub"], "061")
        self.assertEqual(r["cantidad"], 5270.0)


    def test_transferir_producto_generico(self):
        from whatsapp_webhook import _es_traslado_generico_sin_detalle, _texto_item_traslado

        self.assertTrue(_es_traslado_generico_sin_detalle("Transferir producto"))
        self.assertEqual(_texto_item_traslado("Transferir producto").lower(), "producto")

    def test_tortas_bodegas_sin_verbo(self):
        from whatsapp_webhook import (
            _es_mensaje_traslado,
            _es_traslado_implicito,
            _resolver_subreceta_para_traslado,
        )

        t = "5 tortas de choclate de 005 a 001"
        self.assertTrue(_es_traslado_implicito(t))
        self.assertTrue(_es_mensaje_traslado(t))
        sub = _resolver_subreceta_para_traslado(t)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["cod_sub"], "061")
        self.assertAlmostEqual(float(sub["cantidad"]), 5270.0)

    def test_salsa_drunken_no_udon(self):
        from whatsapp_webhook import _match_sub_codigos_en_texto, _resolver_subreceta_para_traslado

        t = "trasladar salsa drunken de 005 a 001"
        cods = _match_sub_codigos_en_texto(t)
        self.assertEqual(cods, ["060"])
        sub = _resolver_subreceta_para_traslado(t)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["cod_sub"], "060")
        self.assertNotIn("udon", (sub.get("nombre") or "").lower())


class TestPeriodoPruebasCocina(unittest.TestCase):
    def test_jacky_en_periodo_pruebas(self):
        from estrategia_config import periodo_pruebas_ignorar_stock

        with patch.dict("os.environ", {"TATAMI_PERIODO_PRUEBAS_COCINA": "1"}):
            from importlib import reload

            import estrategia_config

            reload(estrategia_config)
            self.assertTrue(
                estrategia_config.periodo_pruebas_ignorar_stock("593992911956")
            )

    def test_traslado_stock_insuficiente_permitido(self):
        from whatsapp_webhook import tool_trasladar_mp

        filas = [
            {
                "cod_mp_sistema": "566",
                "nombre_mp": "Whisky Buchanans 18",
                "cod_bodega": "BOD-003",
                "stock_actual": "100",
                "unidad_base": "ml",
            },
            {
                "cod_mp_sistema": "566",
                "nombre_mp": "Whisky Buchanans 18",
                "cod_bodega": "BOD-002",
                "stock_actual": "0",
                "unidad_base": "ml",
            },
        ]
        with patch("whatsapp_webhook.leer_bd_mp_sistema", return_value=filas):
            with patch(
                "whatsapp_webhook._resolver_mp_por_nombre",
                return_value={
                    "ok": True,
                    "cod_mp": "566",
                    "nombre_mp": "Whisky Buchanans 18",
                },
            ):
                with patch(
                    "unidades_operativas.resolver_cantidad_traslado_mp",
                    return_value={"cantidad_base": 750.0, "interpretacion": "750 ml"},
                ):
                    r = tool_trasladar_mp(
                        {
                            "nombre_mp": "buchanan 18",
                            "bodega_origen": "BOD-003",
                            "bodega_destino": "BOD-002",
                            "cantidad": 750,
                            "ignorar_stock": True,
                        }
                    )
        self.assertTrue(r.get("requiere_confirmacion"))
        self.assertIn("periodo de pruebas", r.get("mensaje", "").lower())

    def test_combinar_traslado_generico_con_detalle(self):
        from whatsapp_webhook import _texto_traslado_combinado, _traslado_ctx_touch

        wa = "593992911956"
        _traslado_ctx_touch(wa, texto="trasladar producto")
        out = _texto_traslado_combinado(wa, "2 tortas de chocolate de 005 a 001")
        self.assertIn("005", out)
        self.assertIn("tortas", out.lower())

    def test_continuacion_traslado_no_produccion(self):
        from whatsapp_webhook import (
            _es_continuacion_traslado_pendiente,
            _resolver_prod_sub,
            _traslado_ctx_touch,
        )

        wa = "593992911956"
        _traslado_ctx_touch(wa, texto="trasladar producto")
        self.assertTrue(_es_continuacion_traslado_pendiente("2 torta de chocolate"))
        with patch(
            "whatsapp_webhook._prod_ctx_get",
            return_value={"area": "cocina", "catalog_seen": True},
        ):
            self.assertIsNone(_resolver_prod_sub("2 torta de chocolate", wa))

    def test_cantidad_dos_061(self):
        from whatsapp_webhook import _extraer_cantidad_sub

        with patch(
            "unidades_operativas.cargar_rendimiento_subrecetas",
            return_value={
                "SUB-061": {
                    "rendimiento_estandar": 1054.0,
                    "unidad": "gr",
                    "nombre_subreceta": "torta de chocolate",
                }
            },
        ):
            cant = _extraer_cantidad_sub("2 061", cod_sub="061")
        self.assertAlmostEqual(cant, 2108.0)


class TestGoogleCredentialsPin(unittest.TestCase):
    def test_pin_restores_json_after_dotenv_wipe(self):
        import os
        from pathlib import Path

        from dotenv import load_dotenv
        import google_credentials as gc

        gc._PINNED_ENV["GOOGLE_CREDENTIALS_JSON"] = (
            '{"type":"service_account","project_id":"x"}'
        )
        os.environ["GOOGLE_CREDENTIALS_JSON"] = gc._PINNED_ENV["GOOGLE_CREDENTIALS_JSON"]
        p = Path("_t_pin.env")
        p.write_text("GOOGLE_CREDENTIALS_JSON=\n", encoding="utf-8")
        load_dotenv(p, override=True)
        self.assertFalse((os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip())
        gc.pin_cloud_env()
        self.assertTrue((os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip())
        p.unlink()


class TestProduccionAccesoFelipe(unittest.TestCase):
    def test_socio_puede_producir(self):
        from whatsapp_webhook import _autorizado_produccion_sub

        with patch("whatsapp_webhook.phone_roles", return_value={"SOCIO"}):
            with patch("whatsapp_webhook.autorizado_tool", return_value=False):
                self.assertTrue(_autorizado_produccion_sub("593987122959"))

    def test_producir_cocina_no_es_conteo_con_ctx_activo(self):
        from whatsapp_webhook import _conteo_ctx_touch, _es_mensaje_conteo

        wa = "593987122959"
        _conteo_ctx_touch(wa, active=True)
        self.assertFalse(_es_mensaje_conteo("producir cocina", wa))
        # «cocina» sola con pick producción pendiente no es conteo
        from whatsapp_webhook import _pending_prod_area

        _pending_prod_area[wa] = "pick"
        self.assertFalse(_es_mensaje_conteo("cocina", wa))

    def test_subreceta_activa_pick_area(self):
        from whatsapp_webhook import (
            _es_palabra_subreceta_sola,
            _parse_area_produccion,
            _pending_prod_area,
        )

        self.assertTrue(_es_palabra_subreceta_sola("subreceta"))
        self.assertEqual(_parse_area_produccion("producir cocina"), "cocina")
        wa = "593987122959"
        _pending_prod_area[wa] = "pick"
        self.assertEqual(_pending_prod_area.get(wa), "pick")


class TestAjusteCantidadTraslado(unittest.TestCase):
    def test_parse_ajuste_dos_tortas(self):
        from whatsapp_webhook import _parse_ajuste_cantidad_traslado

        args = {"cod_mp_sistema": "SUB-061"}
        adj = _parse_ajuste_cantidad_traslado("2", args)
        self.assertEqual(adj, {"cantidad_lotes": 2.0})

    def test_parse_ajuste_pero_mejor(self):
        from whatsapp_webhook import _parse_ajuste_cantidad_traslado

        args = {"cod_mp_sistema": "SUB-061"}
        adj = _parse_ajuste_cantidad_traslado("pero 3 tortas", args)
        self.assertEqual(adj, {"cantidad_lotes": 3.0})

    def test_confirmo_no_es_ajuste(self):
        from whatsapp_webhook import _parse_ajuste_cantidad_traslado

        self.assertIsNone(
            _parse_ajuste_cantidad_traslado("confirmo", {"cod_mp_sistema": "SUB-061"})
        )


class TestConfirmacionTraslado(unittest.TestCase):
    def test_confirmo_es_confirmacion_corta(self):
        from whatsapp_webhook import _es_confirmacion_corta

        self.assertTrue(_es_confirmacion_corta("confirmo"))
        self.assertTrue(_es_confirmacion_corta("si confirmo el traslado"))
        self.assertTrue(_es_confirmacion_corta("ai"))

    def test_cancelar_traslado(self):
        from whatsapp_webhook import _es_cancelacion_corta

        self.assertTrue(_es_cancelacion_corta("cancelar"))

    def test_pending_confirm_ttl(self):
        from whatsapp_webhook import (
            _limpiar_ctx_traslado_confirm,
            _traslado_confirm_get,
            _traslado_confirm_touch,
        )

        wa = "593987122959"
        _traslado_confirm_touch(
            wa,
            {
                "bodega_origen": "BOD-005",
                "bodega_destino": "BOD-001",
                "cod_mp_sistema": "SUB-061",
                "nombre_mp": "torta de chocolate",
                "cantidad": 2108,
            },
        )
        self.assertTrue(_traslado_confirm_get(wa))
        _limpiar_ctx_traslado_confirm(wa)
        self.assertFalse(_traslado_confirm_get(wa))

    def test_confirmo_no_va_a_produccion_si_hay_traslado_pendiente(self):
        from whatsapp_webhook import (
            _resolver_prod_sub,
            _traslado_confirm_touch,
        )

        wa = "593987122959"
        _traslado_confirm_touch(wa, {"bodega_origen": "BOD-005", "bodega_destino": "BOD-001"})
        with patch("whatsapp_webhook._prod_ctx_get", return_value={"area": "cocina"}):
            self.assertIsNone(_resolver_prod_sub("confirmo", wa))


if __name__ == "__main__":
    unittest.main()

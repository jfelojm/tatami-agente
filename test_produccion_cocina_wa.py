"""Producción cocina WA: bodega explícita, confirmación y lote sugerido."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import estrategia_config as ec
from whatsapp_webhook import (
    _match_sub_codigos_en_texto,
    _msg_pedir_bodega_produccion,
    _necesita_pedir_bodega_produccion,
    _parse_bodega_produccion_texto,
    _produccion_pendiente_obsoleta,
)

JEFE_COCINA = "593992911956"
STAFF_COCINA = "593983242667"


class TestBodegaProduccionParse(unittest.TestCase):
    def test_005_suelto(self) -> None:
        self.assertEqual(_parse_bodega_produccion_texto("005"), "BOD-005")

    def test_comando_completo(self) -> None:
        bod = _parse_bodega_produccion_texto(
            "Producir 049 3800 005", cod_sub_ignorar="049"
        )
        self.assertEqual(bod, "BOD-005")


class TestRequiereBodegaCocina(unittest.TestCase):
    def setUp(self) -> None:
        ec._phone_to_roles.cache_clear()

    def test_jefe_cocina_dos_bodegas_pide_explicita(self) -> None:
        with patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {"JEFE_COCINA": "ALLOWLIST_JEFE_COCINA"},
            clear=False,
        ), patch.dict(
            "os.environ",
            {"ALLOWLIST_JEFE_COCINA": JEFE_COCINA},
            clear=False,
        ), patch("config_sheets.cfg", return_value=None):
            ec._phone_to_roles.cache_clear()
            self.assertTrue(ec.requiere_bodega_explicita_produccion(JEFE_COCINA))


class TestConfirmacionNoRepreguntaBodega(unittest.TestCase):
    def test_confirmar_con_bodega_ya_elegida(self) -> None:
        prod = {
            "cods": ["049"],
            "bodega": "BOD-005",
            "confirmar": True,
            "bodega_explicita": True,
        }
        self.assertFalse(_necesita_pedir_bodega_produccion(STAFF_COCINA, prod))

    def test_simulacion_pendiente_sin_explicita_repregunta(self) -> None:
        prod = {
            "cods": ["049"],
            "bodega": "BOD-005",
            "confirmar": True,
        }
        with patch.object(ec, "requiere_bodega_explicita_produccion", return_value=True):
            self.assertTrue(_necesita_pedir_bodega_produccion(STAFF_COCINA, prod))

    def test_simulacion_pendiente_con_explicita_ok(self) -> None:
        prod = {
            "cods": ["049"],
            "bodega": "BOD-005",
            "confirmar": True,
            "bodega_explicita": True,
        }
        with patch.object(ec, "requiere_bodega_explicita_produccion", return_value=True):
            self.assertFalse(_necesita_pedir_bodega_produccion(STAFF_COCINA, prod))


class TestMsgPedirBodegaLote(unittest.TestCase):
    @patch(
        "whatsapp_webhook._rendimiento_sub_display",
        return_value=("22", "uni"),
    )
    @patch(
        "whatsapp_webhook._bodegas_opciones_produccion",
        return_value=["BOD-001", "BOD-005"],
    )
    def test_ejemplo_usa_rendimiento_sub(self, _bod, _rend) -> None:
        msg = _msg_pedir_bodega_produccion(
            STAFF_COCINA,
            {"cods": ["072"], "area": "cocina"},
        )
        self.assertIn("22 uni", msg)
        self.assertIn("PRODUCIR SUB 072 22 uni", msg)
        self.assertNotIn("3800", msg)


class TestStockNegativoOperaciones(unittest.TestCase):
    def test_cocina_permite_sin_modo_pruebas(self) -> None:
        with patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {"JEFE_COCINA": "ALLOWLIST_JEFE_COCINA"},
            clear=False,
        ), patch.dict(
            "os.environ",
            {"ALLOWLIST_JEFE_COCINA": JEFE_COCINA, "TATAMI_PERIODO_PRUEBAS_COCINA": "0"},
            clear=False,
        ), patch("config_sheets.cfg", return_value=None):
            ec._phone_to_roles.cache_clear()
            self.assertTrue(ec.permitir_stock_negativo_operaciones(JEFE_COCINA))

    def test_cocina_no_recibe_aviso_stock_negativo(self) -> None:
        with patch.dict(
            ec.ROLE_ALLOWLIST_ENV,
            {
                "JEFE_COCINA": "ALLOWLIST_JEFE_COCINA",
                "STAFF_COCINA": "ALLOWLIST_STAFF_COCINA",
            },
            clear=False,
        ), patch.dict(
            "os.environ",
            {
                "ALLOWLIST_JEFE_COCINA": JEFE_COCINA,
                "ALLOWLIST_STAFF_COCINA": STAFF_COCINA,
            },
            clear=False,
        ), patch("config_sheets.cfg", return_value=None):
            ec._phone_to_roles.cache_clear()
            self.assertFalse(ec.operador_recibe_aviso_stock_negativo(JEFE_COCINA))
            self.assertFalse(ec.operador_recibe_aviso_stock_negativo(STAFF_COCINA))

    def test_filtrar_avisos_stock(self) -> None:
        avisos = [
            "HARINA (001)@BOD-005: stock -1500.0 < consumo 1500.0",
            "MP 001@BOD-005 sin costo (nota)",
        ]
        stock = ec.filtrar_avisos_stock_produccion(avisos)
        self.assertEqual(len(stock), 1)
        self.assertIn("HARINA", stock[0])


class TestCantidadSueltaProduccion(unittest.TestCase):
    @patch(
        "unidades_operativas.cargar_rendimiento_subrecetas",
        return_value={
            "SUB-006": {
                "rendimiento_estandar": 60.0,
                "unidad": "uni",
                "nombre_subreceta": "pan bao",
            }
        },
    )
    def test_bodega_005_61(self, _mock) -> None:
        from whatsapp_webhook import _extraer_cantidad_sub

        cant = _extraer_cantidad_sub("Producir pan bao bodega 005 61", cod_sub="006")
        self.assertEqual(cant, 61.0)

    @patch(
        "unidades_operativas.cargar_rendimiento_subrecetas",
        return_value={
            "SUB-006": {
                "rendimiento_estandar": 60.0,
                "unidad": "uni",
                "nombre_subreceta": "pan bao",
            }
        },
    )
    def test_sub_006_sin_cantidad(self, _mock) -> None:
        from whatsapp_webhook import _extraer_cantidad_sub

        self.assertIsNone(_extraer_cantidad_sub("producir sub 006", cod_sub="006"))

    @patch(
        "unidades_operativas.cargar_rendimiento_subrecetas",
        return_value={
            "SUB-006": {
                "rendimiento_estandar": 60.0,
                "unidad": "uni",
                "nombre_subreceta": "pan bao",
            }
        },
    )
    def test_cantidad_antes_nombre_luis(self, _mock) -> None:
        from whatsapp_webhook import _extraer_cantidad_sub

        cant = _extraer_cantidad_sub(
            "Producir 186 pan bao en bodega 005", cod_sub="006"
        )
        self.assertEqual(cant, 186.0)

    @patch(
        "unidades_operativas.cargar_rendimiento_subrecetas",
        return_value={
            "SUB-006": {
                "rendimiento_estandar": 60.0,
                "unidad": "uni",
                "nombre_subreceta": "pan bao",
            }
        },
    )
    def test_cantidad_antes_en_bodega(self, _mock) -> None:
        from whatsapp_webhook import _extraer_cantidad_sub

        cant = _extraer_cantidad_sub(
            "Producir pan bao 186 en bodega 005", cod_sub="006"
        )
        self.assertEqual(cant, 186.0)

    @patch(
        "unidades_operativas.cargar_rendimiento_subrecetas",
        return_value={
            "SUB-006": {
                "rendimiento_estandar": 60.0,
                "unidad": "uni",
                "nombre_subreceta": "pan bao",
            }
        },
    )
    def test_parse_batch_luis(self, _mock) -> None:
        from whatsapp_webhook import _parse_batch_lenguaje_natural

        batch = _parse_batch_lenguaje_natural(
            "Producir 186 pan bao en bodega 005", "593"
        )
        self.assertEqual(batch.get("cods"), ["006"])
        self.assertEqual(batch.get("bodega"), "BOD-005")
        self.assertEqual(batch.get("cantidad"), 186.0)


class TestResolverProduccionNombre(unittest.TestCase):
    def test_pendiente_obsoleta_si_alias_distinto(self) -> None:
        pending = {"cods": ["004"], "awaiting_bodega": True, "area": "cocina"}
        with patch(
            "whatsapp_webhook._match_sub_codigos_en_texto",
            return_value=["026"],
        ):
            self.assertTrue(
                _produccion_pendiente_obsoleta(
                    pending,
                    "Producir camaron caramelizado",
                    "593987122959",
                )
            )

    def test_pendiente_vigente_solo_bodega(self) -> None:
        pending = {"cods": ["004"], "awaiting_bodega": True, "area": "cocina"}
        self.assertFalse(
            _produccion_pendiente_obsoleta(pending, "005", "593987122959")
        )

    def test_match_camaron_caramelizado_alias(self) -> None:
        cods = _match_sub_codigos_en_texto("Producir camaron caramelizado en 005")
        self.assertIn("026", cods)

    @patch(
        "whatsapp_webhook._resolver_cods_produccion_desde_texto",
        return_value=(["004"], None),
    )
    def test_batch_hamburguesa_resuelve_004(self, _mock) -> None:
        from whatsapp_webhook import _parse_batch_lenguaje_natural

        r = _parse_batch_lenguaje_natural(
            "Producir 3000 de hamburguesa en 005", "593987122959"
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.get("cods"), ["004"])
        self.assertEqual(r.get("cantidad"), 3000.0)


    def test_batch_pistacho_3800gr_bod005(self) -> None:
        from whatsapp_webhook import _match_sub_codigos_en_texto, _parse_batch_lenguaje_natural

        self.assertEqual(
            _match_sub_codigos_en_texto(
                "Producir 3800gr de pistacho en 005", "593987122959"
            ),
            ["049"],
        )
        r = _parse_batch_lenguaje_natural(
            "Producir 3800gr de pistacho en 005", "593987122959"
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.get("cods"), ["049"])
        self.assertEqual(r.get("bodega"), "BOD-005")
        self.assertEqual(r.get("cantidad"), 3800.0)


if __name__ == "__main__":
    unittest.main()

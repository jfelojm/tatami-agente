"""Tests staging_sync_desde_maestro."""

import unittest
from unittest.mock import patch

from staging_sync_desde_maestro import sync_staging_desde_maestro


class TestStagingSyncDesdeMaestro(unittest.TestCase):
    def test_dry_run_no_escribe_staging(self) -> None:
        with patch("staging_sync_desde_maestro._sync_sub_pseudo_mp") as m_sub:
            m_sub.return_value = {"creadas": 0, "actualizadas": 1}
            res = sync_staging_desde_maestro(dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["traslados"]["items"], "(dry-run)")
        m_sub.assert_called_once()

    @patch("staging_sync_desde_maestro.actualizar_catalogo_factura")
    @patch("staging_sync_desde_maestro.actualizar_catalogo_traslado")
    @patch("staging_sync_desde_maestro._sync_sub_pseudo_mp")
    def test_solo_catalogos(self, m_sub, m_tr, m_fm) -> None:
        m_sub.return_value = {"creadas": 1, "actualizadas": 2}
        m_tr.return_value = {"items": 10, "lista_h": 8, "por_bodega": {"BOD-001": 10}}
        m_fm.return_value = {"items": 5, "por_proveedor": {"161": 5}}
        res = sync_staging_desde_maestro(dry_run=False, skip_sub_sync=False)
        self.assertEqual(res["modo"], "solo-catalogos")
        self.assertEqual(res["traslados"]["items"], 10)
        m_tr.assert_called_once()
        m_fm.assert_called_once()


if __name__ == "__main__":
    unittest.main()

"""Tests para parseo de confirmación SI / DUDA / SOLO."""
from factura_confirmacion_parse import (
    parse_confirmacion_factura,
    parse_lineas_spec,
    resolver_lineas_duda,
)


def test_parse_si_simple():
    assert parse_confirmacion_factura("si")["action"] == "apply"
    assert parse_confirmacion_factura("SÍ")["lineas_duda"] == set()


def test_parse_no():
    assert parse_confirmacion_factura("no")["action"] == "cancel"


def test_parse_duda():
    p = parse_confirmacion_factura("SI DUDA 3,5")
    assert p["action"] == "apply"
    assert p["lineas_duda"] == {3, 5}


def test_parse_si_solo_numeros():
    """SI 1,8,9,11 = SOLO esas líneas (no DUDA)."""
    p = parse_confirmacion_factura("Si 1,8,9,11")
    assert p["action"] == "apply"
    assert p["lineas_solo"] == {1, 8, 9, 11}
    assert p["lineas_duda"] == set()


def test_parse_duda_rango():
    p = parse_confirmacion_factura("si duda 2-4")
    assert p["lineas_duda"] == {2, 3, 4}


def test_parse_solo():
    p = parse_confirmacion_factura("SI SOLO 1,2")
    assert p["lineas_solo"] == {1, 2}


def test_parse_lineas_spec():
    assert parse_lineas_spec("1-3,7") == {1, 2, 3, 7}


def test_resolver_solo():
    todas = {1, 2, 3, 4}
    duda = resolver_lineas_duda(set(), {1, 2}, todas)
    assert duda == {3, 4}

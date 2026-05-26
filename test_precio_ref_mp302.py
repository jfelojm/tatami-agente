"""Regresión MP 302 espumante: factor 9750 (pack) y 750 (botella) sin doble división."""
from numeros_sheets import precio_ref_a_unidad_base


def _ciclo_factura(costo_efectivo: float, factor: float) -> tuple[float, float]:
    """Simula procesar_facturas (escritura) + costo_mp_canonico (lectura)."""
    precio_ref = round(costo_efectivo / factor, 6)
    cu_motor = precio_ref_a_unidad_base(precio_ref, factor)
    return precio_ref, cu_motor


def test_combo_9750_pack():
    pr, cu = _ciclo_factura(87.64, 9750.0)
    assert 0.008 <= pr <= 0.012
    assert cu == pr


def test_botella_750():
    pr, cu = _ciclo_factura(7.341667, 750.0)
    assert 0.008 <= pr <= 0.012
    assert cu == pr


def test_catalogo_actual_combo():
    assert precio_ref_a_unidad_base(0.009789, 9750.0) == 0.009789


def test_catalogo_actual_favorita():
    assert precio_ref_a_unidad_base(0.010782, 750.0) == 0.010782


def test_legacy_inflado_no_redivide_peor():
    """0.116853 era pack÷750 mal guardado; lectura no debe bajar a ~0.00015."""
    cu = precio_ref_a_unidad_base(0.116853, 750.0)
    assert cu == 0.116853  # detectar manualmente; no empeorar con 2ª división


if __name__ == "__main__":
    test_combo_9750_pack()
    test_botella_750()
    test_catalogo_actual_combo()
    test_catalogo_actual_favorita()
    test_legacy_inflado_no_redivide_peor()
    print("ok MP302")

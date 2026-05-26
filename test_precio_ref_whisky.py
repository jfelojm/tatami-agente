"""Regresión: precio_ref unitario desde facturas no debe re-dividirse por factor."""
from numeros_sheets import precio_ref_a_unidad_base


def test_jw_gold():
    assert precio_ref_a_unidad_base(0.098653, 750) == 0.098653


def test_jw_black():
    assert precio_ref_a_unidad_base(0.052467, 750) == 0.052467


def test_pack_manual_200g():
    assert precio_ref_a_unidad_base(0.15, 200) == 0.00075


def test_gaseosa_pack_ya_unitario():
    assert precio_ref_a_unidad_base(0.242222, 12) == 0.242222
    assert precio_ref_a_unidad_base(0.278, 24) == 0.278


if __name__ == "__main__":
    test_jw_gold()
    test_jw_black()
    test_pack_manual_200g()
    test_gaseosa_pack_ya_unitario()
    print("ok")

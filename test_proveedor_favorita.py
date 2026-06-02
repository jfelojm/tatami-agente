"""Routing Supermaxi (136) vs Titán (175) por estab en factura Favorita."""

from proveedor_favorita import (
    COD_PROVEEDOR_SUPERMAXI,
    COD_PROVEEDOR_TITAN,
    RUC_CORPORACION_FAVORITA,
    formato_compra_para_factura,
    resolver_cod_proveedor_factura,
)


def test_lookup_default_supermaxi():
    from procesar_facturas_drive import cargar_lookup_ruc

    cargar_lookup_ruc.__globals__["_prov_ruc_cache"] = None
    lookup = cargar_lookup_ruc()
    assert lookup[RUC_CORPORACION_FAVORITA] == COD_PROVEEDOR_SUPERMAXI


def test_estab_219_titan():
    cod = resolver_cod_proveedor_factura(
        RUC_CORPORACION_FAVORITA, "219-105-000035874"
    )
    assert cod == COD_PROVEEDOR_TITAN


def test_estab_016_supermaxi():
    cod = resolver_cod_proveedor_factura(
        RUC_CORPORACION_FAVORITA, "016-105-000706428"
    )
    assert cod == COD_PROVEEDOR_SUPERMAXI


def test_estab_025_supermaxi():
    cod = resolver_cod_proveedor_factura(
        RUC_CORPORACION_FAVORITA, "025-104-000413664"
    )
    assert cod == COD_PROVEEDOR_SUPERMAXI


def test_formato_compra_solo_favorita():
    assert formato_compra_para_factura(RUC_CORPORACION_FAVORITA, "219-1-1") == "TITAN"
    assert (
        formato_compra_para_factura(RUC_CORPORACION_FAVORITA, "016-1-1") == "SUPERMAXI"
    )
    assert formato_compra_para_factura("0992613092001", "016-1-1") == ""
    assert formato_compra_para_factura("", "219-1-1") == ""


if __name__ == "__main__":
    test_lookup_default_supermaxi()
    test_estab_219_titan()
    test_estab_016_supermaxi()
    test_estab_025_supermaxi()
    test_formato_compra_solo_favorita()
    print("OK proveedor_favorita")

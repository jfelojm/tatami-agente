"""Routing Supermaxi (136) vs Titán (175) por estab en factura Favorita."""

from proveedor_favorita import (
    COD_PROVEEDOR_SUPERMAXI,
    COD_PROVEEDOR_TITAN,
    RUC_CORPORACION_FAVORITA,
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


if __name__ == "__main__":
    test_lookup_default_supermaxi()
    test_estab_219_titan()
    test_estab_016_supermaxi()
    test_estab_025_supermaxi()
    print("OK proveedor_favorita")

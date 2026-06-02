"""
Fórmulas para Google Sheets con locale español (Ecuador/LATAM).

Separador de argumentos: punto y coma (;).
Funciones: SI, SI.ERROR, INDICE, COINCIDIR, HIPERVINCULO.

Override: TATAMI_SHEETS_FORMULA_LOCALE=en para entornos en inglés.
"""

from __future__ import annotations

import os

_ARG_SEP = ";" if (os.getenv("TATAMI_SHEETS_FORMULA_LOCALE") or "es").lower() != "en" else ","


def _sep() -> str:
    return _ARG_SEP


def formula_pendientes_nombre_mp(
    col_mp: str, col_nom_bd: str, col_cod_bd: str, row: int
) -> str:
    """Nombre MP desde BD_MP_SISTEMA según cod_mp_asignado."""
    s = _sep()
    p, r = col_mp, row
    return (
        f'=SI(${p}{r}=""{s} ""{s} SI.ERROR(INDICE(BD_MP_SISTEMA!${col_nom_bd}:${col_nom_bd}{s} '
        f'COINCIDIR(${p}{r}{s} BD_MP_SISTEMA!${col_cod_bd}:${col_cod_bd}{s} 0){s} "NO EN BD"))'
    )


def formula_pendientes_unidad_base(
    col_mp: str, col_uni_bd: str, col_cod_bd: str, row: int
) -> str:
    s = _sep()
    p, r = col_mp, row
    return (
        f'=SI(${p}{r}=""{s} ""{s} SI.ERROR(INDICE(BD_MP_SISTEMA!${col_uni_bd}:${col_uni_bd}{s} '
        f'COINCIDIR(${p}{r}{s} BD_MP_SISTEMA!${col_cod_bd}:${col_cod_bd}{s} 0){s} ""))'
    )


def formula_pendientes_link_xml(col_drive: str, row: int) -> str:
    s = _sep()
    d, r = col_drive, row
    return (
        f'=SI({d}{r}=""{s} ""{s} HIPERVINCULO("https://drive.google.com/file/d/" & '
        f'{d}{r} & "/view"{s} "Ver XML"))'
    )


def formula_pendientes_ref_columna(col: str, row: int) -> str:
    """Referencia simple a otra columna de la misma fila (plantilla)."""
    return f"={col}{row}"

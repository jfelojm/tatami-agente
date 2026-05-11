"""
Interpretación de números tal como Google Sheets / gspread devuelve en get_all_values().

En BD_MP_SISTEMA suele mezclarse:
- Celdas numéricas con formato regional ES: decimal con coma (ej. 244,7158).
- Celdas o fórmulas con punto como decimal (ej. 20366.0632).
- Valores con separador de miles: 1.234,56 (EU) o 1,234.56 (US).

El agente históricamente solo hacía str.replace(",", "."), lo cual rompe casos EU con miles.

Regla aplicada:
- Solo coma → coma es decimal.
- Solo punto → punto es decimal (si hay un solo punto) o miles EU (varios puntos, sin coma): se quitan puntos.
- Coma y punto → el separador más a la derecha es el decimal; el otro son miles.
"""

from __future__ import annotations


def parse_sheet_number(v, default: float = 0.0) -> float:
    if v is None:
        return default
    s = str(v).strip()
    if not s or s in {"-", "—"}:
        return default

    for ch in ("\u00a0", "\u2007", "\u2009", "\u202f"):
        s = s.replace(ch, "")
    s = s.replace(" ", "")

    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    if s.startswith("-"):
        neg = not neg
        s = s[1:].strip()
    if not s:
        return default

    n_comma = s.count(",")
    n_dot = s.count(".")

    if n_comma == 0 and n_dot == 0:
        try:
            x = float(s)
        except ValueError:
            return default
        return -x if neg else x

    if n_comma >= 1 and n_dot == 0:
        if n_comma == 1:
            t = s.replace(",", ".")
        else:
            t = s.replace(",", "")
        try:
            x = float(t)
        except ValueError:
            return default
        return -x if neg else x

    if n_dot >= 1 and n_comma == 0:
        if n_dot == 1:
            t = s
        else:
            t = s.replace(".", "")
        try:
            x = float(t)
        except ValueError:
            return default
        return -x if neg else x

    ic = s.rfind(",")
    idot = s.rfind(".")
    if ic > idot:
        t = s.replace(".", "").replace(",", ".")
    else:
        t = s.replace(",", "")
    try:
        x = float(t)
    except ValueError:
        return default
    return -x if neg else x

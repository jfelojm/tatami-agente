"""
Códigos canónicos de subreceta: SUB-051 (alineado a BD_MP_SISTEMA).

BD_SUBRECETAS y detalle deben usar el mismo prefijo que pseudo_mp_cod().
"""

from __future__ import annotations

PREFIJO = "SUB-"


def cod_sub_canonico(cod: str) -> str:
    """051 | SUB-051 | sub-51 → SUB-051"""
    s = (cod or "").strip().upper()
    if not s:
        return ""
    if s.startswith(PREFIJO):
        body = s[len(PREFIJO) :].lstrip("0") or "0"
        if body.isdigit():
            return f"{PREFIJO}{int(body):03d}"
        return f"{PREFIJO}{body}"
    if s.isdigit():
        return f"{PREFIJO}{int(s):03d}"
    return f"{PREFIJO}{s}"


def cod_sub_sin_prefijo(cod: str) -> str:
    """SUB-051 → 051 (solo dígitos, 3 cifras si aplica)."""
    s = (cod or "").strip().upper()
    if s.startswith(PREFIJO):
        s = s[len(PREFIJO) :]
    if s.isdigit():
        return str(int(s)).zfill(3)
    return s


def mismos_cod_sub(a: str, b: str) -> bool:
    return cod_sub_canonico(a) == cod_sub_canonico(b)

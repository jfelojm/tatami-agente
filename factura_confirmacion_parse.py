"""Parsea respuestas de confirmación de factura por WhatsApp (SI / NO / DUDA / SOLO)."""
from __future__ import annotations

import re
import unicodedata


def _norm(texto: str) -> str:
    t = (texto or "").strip().upper()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"\s+", " ", t)
    return t


def parse_lineas_spec(spec: str) -> set[int]:
    """
    Convierte '1,3,5-7 10' en {1,3,5,6,7,10}.
    """
    out: set[int] = set()
    if not spec or not str(spec).strip():
        return out
    for part in re.split(r"[,;\s]+", str(spec).strip()):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a.strip()), int(b.strip())
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def parse_confirmacion_factura(texto: str) -> dict:
    """
    Retorna:
      action: 'cancel' | 'apply' | 'invalid'
      lineas_duda: set[int]
      lineas_solo: set[int] | None  (si no None, el resto de líneas numeradas va a duda)
    """
    t = _norm(texto)
    if t in ("NO", "CANCELAR", "CANCELA"):
        return {"action": "cancel", "lineas_duda": set(), "lineas_solo": None}

    if not t:
        return {"action": "invalid", "lineas_duda": set(), "lineas_solo": None}

    # Quitar prefijo SI / SÍ / OK
    rest = re.sub(r"^(SI|SÍ|OK|CONFIRMAR)\s*", "", t).strip()
    if not rest and t in ("SI", "SÍ", "OK", "CONFIRMAR"):
        return {"action": "apply", "lineas_duda": set(), "lineas_solo": None}

    lineas_duda: set[int] = set()
    lineas_solo: set[int] | None = None

    m_solo = re.search(r"\bSOLO\b\s*[: ]?\s*(.+)$", rest)
    if m_solo:
        lineas_solo = parse_lineas_spec(m_solo.group(1))
        rest = rest[: m_solo.start()].strip()

    for pat in (
        r"\b(?:DUDA|DUDOSO|REVISAR)\b\s*[: ]?\s*(.+)$",
        r"\b(?:EXCLUIR|OMITIR|SIN)\b\s*[: ]?\s*(.+)$",
    ):
        m = re.search(pat, rest)
        if m:
            lineas_duda |= parse_lineas_spec(m.group(1))
            rest = rest[: m.start()].strip()
            break

    # "SI 1,8,11" sin DUDA → solo esas líneas a inventario (resto a pendientes)
    if not lineas_duda and not lineas_solo and rest and re.fullmatch(r"[\d,\s\-]+", rest):
        lineas_solo = parse_lineas_spec(rest)
        rest = ""

    if t.startswith(("SI", "SÍ", "OK", "CONFIRMAR")) or lineas_duda or lineas_solo:
        if rest and rest not in ("", "OK"):
            # Texto extra no reconocido
            if not lineas_duda and not lineas_solo:
                return {"action": "invalid", "lineas_duda": set(), "lineas_solo": None}
        return {"action": "apply", "lineas_duda": lineas_duda, "lineas_solo": lineas_solo}

    return {"action": "invalid", "lineas_duda": set(), "lineas_solo": None}


def lineas_aplicables_factura_dict(factura_dict: dict) -> tuple[list[dict], set[int]]:
    """
    Asigna campo 'linea' (1-based) a ítems con cantidad y precio.
    Retorna (items_actualizados, set de números de línea).
    """
    items = list(factura_dict.get("items") or [])
    n = 0
    numeros: set[int] = set()
    for item in items:
        cant = item.get("cantidad")
        price = item.get("precio_total") or item.get("precio_total_sin_impuesto")
        if cant and price:
            n += 1
            item["linea"] = n
            numeros.add(n)
        else:
            item.pop("linea", None)
    factura_dict["items"] = items
    return items, numeros


def resolver_lineas_duda(
    lineas_duda: set[int],
    lineas_solo: set[int] | None,
    todas_las_lineas: set[int],
) -> set[int]:
    """Combina DUDA explícita con SOLO (el resto pasa a duda)."""
    duda = set(lineas_duda or set())
    if lineas_solo is not None:
        solo = set(lineas_solo)
        duda |= todas_las_lineas - solo
    return duda

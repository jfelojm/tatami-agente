"""
Parseo y escritura de números desde/hacia Google Sheets (locale es-EC).

Usar siempre parse_numero_sheets() al leer celdas con coma decimal o miles con punto.
"""

from __future__ import annotations

# Costo referencial único por MP (misma cifra en todas las bodegas del maestro).
BODEGA_COSTO_CANONICA = "BOD-001"


def parse_numero_sheets(v, default: float = 0.0) -> float:
    """
    Convierte texto de Sheets a float.

    Soporta:
      - 0,005293 / 0.005293
      - 20.408,42 (miles con punto, decimal con coma)
      - 1.630,46
      - 110 / 42,25
    """
    try:
        s = str(v or "").strip()
        if not s:
            return default
        s = s.replace("\u00a0", "").replace(" ", "")

        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        elif s.count(".") > 1:
            parts = s.split(".")
            if all(p.isdigit() for p in parts):
                if len(parts[-1]) == 3 and len(parts) >= 2:
                    s = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) <= 2 else "".join(parts)
                    if len(parts[-1]) == 3:
                        s = "".join(parts)
                else:
                    s = "".join(parts[:-1]) + "." + parts[-1]

        return float(s)
    except (TypeError, ValueError):
        return default


def _costos_validos_mp(
    costos_por_bodega: dict[tuple[str, str], float],
    cod_mp_norm: str,
) -> list[tuple[str, float]]:
    """Lista (bodega, costo) > 0 para un MP, excluyendo precios pack obvios."""
    items = [
        (bod, v)
        for (mp, bod), v in costos_por_bodega.items()
        if mp == cod_mp_norm and v > 0
    ]
    if len(items) < 2:
        return items
    vals = sorted(v for _, v in items)
    min_v = vals[0]
    filtrados = []
    for bod, v in items:
        if min_v > 0 and v > min_v * 5 and min_v < 0.5 and v > 1.0:
            continue
        filtrados.append((bod, v))
    return filtrados or items


def canonical_costo_por_mp(
    costos_por_bodega: dict[tuple[str, str], float],
) -> dict[str, float]:
    """
    Un solo USD/unidad_base por cod_mp_norm.
    Prioridad: BOD-001; si no hay, el menor costo plausible entre bodegas.
    """
    from bodegas_config import normalizar_cod_bodega

    mps = {mp for (mp, _), v in costos_por_bodega.items() if v > 0}
    out: dict[str, float] = {}
    canon_bod = normalizar_cod_bodega(BODEGA_COSTO_CANONICA)

    for nk in mps:
        validos = _costos_validos_mp(costos_por_bodega, nk)
        if not validos:
            continue
        por_bod = {normalizar_cod_bodega(b): v for b, v in validos}
        if canon_bod in por_bod:
            out[nk] = round(por_bod[canon_bod], 6)
        else:
            out[nk] = round(min(por_bod.values()), 6)
    return out


def elegir_costo_unitario_mp(
    costos_por_bodega: dict[tuple[str, str], float],
    cod_mp_norm: str,
    cod_bodega: str,
) -> tuple[float, str]:
    """
    Mismo costo unitario para todas las bodegas de un MP (política Tatami actual).
    La bodega en la línea de receta solo define inventario, no el precio ref.
    """
    nk = (cod_mp_norm or "").strip()
    if not nk:
        return 0.0, "sin_cod_mp"

    canon = canonical_costo_por_mp(costos_por_bodega)
    cu = canon.get(nk, 0.0)
    if cu > 0:
        return cu, ""
    return 0.0, "sin_costo_mp"


def expandir_costos_mp_unico(
    costos_por_bodega: dict[tuple[str, str], float],
) -> dict[tuple[str, str], float]:
    """Replica el costo canónico en cada (mp, bodega) presente en el mapa."""
    canon = canonical_costo_por_mp(costos_por_bodega)
    out: dict[tuple[str, str], float] = {}
    for (mp, bod) in costos_por_bodega:
        if mp in canon:
            out[(mp, bod)] = canon[mp]
    for (mp, bod), v in costos_por_bodega.items():
        if (mp, bod) not in out and mp in canon:
            out[(mp, bod)] = canon[mp]
    return out


def precio_ref_a_unidad_base(precio: float, fac: float) -> float:
    """
    USD por unidad_base desde precio_ref de BD_ITEMS_PROV.

    Contrato facturas (procesar_facturas_drive): precio_ref YA es costo_efectivo÷factor
    (USD por gr/ml/uni). El factor en la hoja describe el pack de compra, no vuelve a dividir.

    Solo dividir precio/factor si la celda trae precio de BULTO (ej. 0,15 USD por 200 g).
    """
    if precio <= 0:
        return 0.0
    if fac <= 1:
        return round(precio, 6)

    # Botella/lata desde XML: precio (USD/u) × fac (ml/gr) ≈ 8–500 USD; precio en banda ml/gr.
    implied_pack = precio * fac
    if (
        precio < 1.0
        and 8.0 <= implied_pack <= 500.0
        and 0.0005 <= precio <= 0.25
        and fac >= 300
    ):
        return round(precio, 6)

    # Whisky/vinos ml estándar: 0,04–0,25 USD/ml con fac 330–1000
    if fac >= 300 and 0.0005 <= precio <= 0.25:
        return round(precio, 6)

    # Licores premium ml (Blue Label ~0,54): botella ~50–500 USD, no re-dividir
    if fac >= 300 and 0.25 < precio <= 0.65 and 50.0 <= implied_pack <= 500.0:
        return round(precio, 6)

    # Gaseosas/latas: precio_ref ya es USD por botella (XML pack ÷ 12/24); factor = uni del pack.
    if fac in (6, 8, 12, 15, 18, 24, 30, 48) and 0.08 <= precio <= 2.0:
        implied_pack = precio * fac
        if 2.0 <= implied_pack <= 80.0:
            return round(precio, 6)

    # Rango típico precio_ref unitario barato (insumos gr/ml)
    if precio <= 0.08:
        return round(precio, 6)

    cu_div = precio / fac
    if precio >= 0.08 and cu_div < precio / 10.0:
        return round(cu_div, 6)
    if implied_pack > 50:
        return round(cu_div, 6)
    if precio > max(1.0, fac * 0.08):
        return round(cu_div, 6)
    return round(precio, 6)


# Alias retrocompatible
_safe_float = parse_numero_sheets

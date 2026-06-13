"""Utilidades de períodos, comparativos y acumulados para dashboards."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta


def parse_fecha(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(str(raw).strip()[:10])


def periodo_anterior(desde: date, hasta: date) -> tuple[date, date]:
    """Período de comparación: mes/año calendario anterior si aplica; si no, misma duración."""
    from calendar import monthrange

    if desde.day == 1:
        ultimo = monthrange(desde.year, desde.month)[1]
        if hasta == date(desde.year, desde.month, ultimo):
            if desde.month == 1:
                py, pm = desde.year - 1, 12
            else:
                py, pm = desde.year, desde.month - 1
            plast = monthrange(py, pm)[1]
            return date(py, pm, 1), date(py, pm, plast)

    if (
        desde.month == 1
        and desde.day == 1
        and hasta.month == 12
        and hasta.day == 31
        and hasta.year == desde.year
    ):
        py = desde.year - 1
        return date(py, 1, 1), date(py, 12, 31)

    dias = (hasta - desde).days + 1
    fin_ant = desde - timedelta(days=1)
    ini_ant = fin_ant - timedelta(days=dias - 1)
    return ini_ant, fin_ant


def mismo_periodo_anio_anterior(desde: date, hasta: date) -> tuple[date, date]:
    """Mismo rango calendario un año atrás (ajusta 29-feb)."""

    def shift(d: date) -> date:
        try:
            return d.replace(year=d.year - 1)
        except ValueError:
            return d.replace(year=d.year - 1, day=28)

    return shift(desde), shift(hasta)


def acumulado_anio(hasta: date) -> tuple[date, date]:
    """YTD: 1 ene del año de `hasta` hasta `hasta`."""
    return date(hasta.year, 1, 1), hasta


def mes_completo(anio: int, mes: int) -> tuple[date, date]:
    ultimo = monthrange(anio, mes)[1]
    return date(anio, mes, 1), date(anio, mes, ultimo)


def delta_pct(actual: float, base: float) -> float | None:
    if base == 0:
        return None if actual == 0 else 100.0
    return round((actual - base) / base * 100, 1)


def resumen_comparativo(actual: dict, base: dict) -> dict:
    """Compara métricas numéricas entre dos dicts."""
    out: dict = {}
    for key in ("vta", "uds", "ticket", "descuentos"):
        a = float(actual.get(key) or 0)
        b = float(base.get(key) or 0)
        out[key] = {
            "actual": round(a, 2),
            "base": round(b, 2),
            "delta": round(a - b, 2),
            "delta_pct": delta_pct(a, b),
        }
    return out

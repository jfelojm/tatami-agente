"""Score de confianza de inventario por bodega."""

from __future__ import annotations

from datetime import date, datetime

from bodegas_config import BODEGAS, normalizar_cod_bodega
from dashboard_services.inventario_vivo import BODEGAS_DASH


def _parse_dt(raw: object) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:19])
    except ValueError:
        return None


def build_confianza_inventario(
    *,
    ciclos: list[dict],
    detalles: list[dict],
    stock_negativos: int,
    hoy: date | None = None,
) -> dict:
    hoy = hoy or date.today()
    por_bodega: dict[str, dict] = {}

    for label, cod in BODEGAS_DASH.items():
        ultimo = None
        for c in ciclos:
            if normalizar_cod_bodega(c.get("cod_bodega")) != cod:
                continue
            if (c.get("estado") or "").upper() != "CONTABILIZADO":
                continue
            dt = _parse_dt(c.get("updated_at") or c.get("created_at") or c.get("snapshot_at"))
            if dt and (ultimo is None or dt > ultimo):
                ultimo = dt
        dias = (hoy - ultimo.date()).days if ultimo else 999
        frescura = max(0.0, 100.0 - dias * 3.33) if dias < 30 else 0.0

        dets_bod = [d for d in detalles if normalizar_cod_bodega(d.get("cod_bodega")) == cod]
        if dets_bod:
            ok = sum(
                1
                for d in dets_bod
                if abs(float(d.get("delta_calculado") or 0) - float(d.get("delta_esperado") or d.get("delta_calculado") or 0)) < 0.01
            )
            precision = ok / len(dets_bod) * 100
        else:
            precision = 50.0

        drift = 70.0
        neg_pen = max(0.0, 100.0 - stock_negativos * 5)
        score = round(frescura * 0.3 + precision * 0.4 + drift * 0.2 + neg_pen * 0.1, 1)

        por_bodega[label] = {
            "cod_bodega": cod,
            "nombre": BODEGAS[cod].nombre if cod in BODEGAS else label,
            "score": score,
            "dias_ultimo_conteo": dias if ultimo else None,
            "frescura_pct": round(frescura, 1),
            "precision_delta_pct": round(precision, 1),
            "stock_negativos": stock_negativos,
        }

    global_score = round(sum(b["score"] for b in por_bodega.values()) / max(len(por_bodega), 1), 1)
    return {"score_global": global_score, "bodegas": por_bodega}

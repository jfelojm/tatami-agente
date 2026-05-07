import os
import argparse
from datetime import datetime

from dotenv import load_dotenv

from ventas_smartmenu import descargar_ventas_grid

load_dotenv(override=True)


def _safe_float(v: str) -> float:
    try:
        return float(str(v).strip().replace(",", "."))
    except Exception:
        return 0.0


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def calcular_total_smartmenu(
    fecha: str,
    desde_hora: str | None = None,
    hasta_hora: str | None = None,
    sin_iva: bool = True,
):
    """
    Suma el total por DOCUMENTO desde el grid Smart Menu (comprasloadVentas.php).

    Columnas (según observado en el grid):
      - row[3] = 'YYYY-MM-DD HH:MM:SS' (fecha/hora documento)
      - row[7] = total documento (con IVA)
      - row[13] = subtotal (sin IVA)
    """
    rows = descargar_ventas_grid(fecha)
    if not rows:
        return {"fecha": fecha, "docs": 0, "total": 0.0}

    dt_desde = _parse_dt(f"{fecha} {desde_hora}:00") if desde_hora else None
    dt_hasta = _parse_dt(f"{fecha} {hasta_hora}:00") if hasta_hora else None

    total = 0.0
    docs = 0
    for r in rows:
        dt = _parse_dt(r[3]) if len(r) > 3 else None
        if dt_desde and dt and dt < dt_desde:
            continue
        if dt_hasta and dt and dt > dt_hasta:
            continue
        if sin_iva:
            total += _safe_float(r[13] if len(r) > 13 else "0")
        else:
            total += _safe_float(r[7] if len(r) > 7 else "0")
        docs += 1

    return {"fecha": fecha, "docs": docs, "total": round(total, 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Total de ventas (grid Smart Menu) por rango horario.")
    parser.add_argument("--fecha", help="YYYY-MM-DD. Si no se envía, usa hoy.", default=None)
    parser.add_argument("--desde", help="HH:MM (opcional)", default=None)
    parser.add_argument("--hasta", help="HH:MM (opcional)", default=None)
    parser.add_argument(
        "--modo",
        choices=["sin_iva", "con_iva"],
        default="sin_iva",
        help="sin_iva=subtotal sin IVA (default), con_iva=total con IVA",
    )
    args = parser.parse_args()

    fecha = args.fecha or datetime.now().strftime("%Y-%m-%d")
    sin_iva = args.modo != "con_iva"

    res = calcular_total_smartmenu(
        fecha, desde_hora=args.desde, hasta_hora=args.hasta, sin_iva=sin_iva
    )
    etiqueta = "SUBTOTAL sin IVA" if sin_iva else "TOTAL con IVA"
    rango = ""
    if args.desde or args.hasta:
        rango = f" [{args.desde or '00:00'}-{args.hasta or '23:59'}]"
    print(f"Smart Menu {etiqueta} {res['fecha']}{rango} ({res['docs']} docs): ${res['total']}")


import os
import argparse
from datetime import datetime

from dotenv import load_dotenv

from ventas_smartmenu import (
    _estado_documento_desde_texto_grid,
    _parse_descuento_valor,
    descargar_ventas_grid,
)

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


def _grid_documento_excluido_neto(row: list[str]) -> bool:
    """Col 9: solo ANULADO excluye del total oficial (notas NO AUTORIZADO sí suman)."""
    raw = row[9] if len(row) > 9 else ""
    return _estado_documento_desde_texto_grid(raw) == "ANULADO"


def _grid_descuento_documento(row: list[str]) -> float:
    """
    Descuento a nivel documento (Ventas Brutas − Descuentos = Ventas Netas en Smart Menu).
    Grid comprasloadVentas: col 16 = valorDescuento (ver scriptVentas.js setColumnIds).
    """
    return _parse_descuento_valor(row[16] if len(row) > 16 else "")


def _grid_subtotal_sin_iva_cuadre(row: list[str]) -> float:
    """
    Subtotal sin IVA para cuadrar con hist_ventas (suma de líneas del detalle).

    En varias instalaciones Smart Menu, NOTA / no autorizado dejan col 13 en 0 pero
    col 7 trae el monto cobrado que sí coincide con el detalle guardado en hist.
    """
    sub = _safe_float(row[13] if len(row) > 13 else "0")
    tot = _safe_float(row[7] if len(row) > 7 else "0")
    tipo = (row[2] if len(row) > 2 else "").strip().upper()
    raw9 = row[9] if len(row) > 9 else ""
    if sub > 0.0001:
        return sub
    if tot <= 0.0001:
        return 0.0
    # Nota efectivo / sin factura: col 7 suele alinear con suma de líneas (hist subtotal).
    if "NOTA" in tipo or _estado_documento_desde_texto_grid(raw9) == "NO_AUTORIZADO":
        return tot
    iva = float((os.getenv("SMART_MENU_IVA_DECIMAL") or "0.12").replace(",", "."))
    return tot / (1.0 + max(0.0, iva))


def calcular_total_smartmenu(
    fecha: str,
    desde_hora: str | None = None,
    hasta_hora: str | None = None,
    sin_iva: bool = True,
    incluir_anulados: bool = False,
):
    """
    Suma el total por DOCUMENTO desde el grid Smart Menu (comprasloadVentas.php).

    Columnas (según observado en el grid):
      - row[3] = 'YYYY-MM-DD HH:MM:SS' (fecha/hora documento)
      - row[7] = total documento (con IVA)
      - row[9] = estado en grid (ANULADO excluye del total; NO AUTORIZADO = nota operativa, sí suma)
      - row[13] = subtotal (sin IVA); si viene 0 en NOTA / NO_AUTORIZADO, se usa row[7] para cuadre.

    Por defecto **no** suma documentos anulados (notas NO AUTORIZADO sí suman: efectivo / sin factura).
    """
    rows = descargar_ventas_grid(fecha)
    if not rows:
        return {
            "fecha": fecha,
            "docs": 0,
            "docs_anulados": 0,
            "total_bruto": 0.0,
            "total_descuentos": 0.0,
            "total": 0.0,
        }

    dt_desde = _parse_dt(f"{fecha} {desde_hora}:00") if desde_hora else None
    dt_hasta = _parse_dt(f"{fecha} {hasta_hora}:00") if hasta_hora else None

    total_bruto = 0.0
    total_descuentos = 0.0
    docs = 0
    docs_anulados = 0
    for r in rows:
        dt = _parse_dt(r[3]) if len(r) > 3 else None
        if dt_desde and dt and dt < dt_desde:
            continue
        if dt_hasta and dt and dt > dt_hasta:
            continue
        if not incluir_anulados and _grid_documento_excluido_neto(r):
            docs_anulados += 1
            continue
        if sin_iva:
            total_bruto += _grid_subtotal_sin_iva_cuadre(r)
            total_descuentos += _grid_descuento_documento(r)
        else:
            total_bruto += _safe_float(r[7] if len(r) > 7 else "0")
            # Descuento en reporte Smart Menu es sobre base sin IVA; no restar de total con IVA aquí.
        docs += 1

    total_neto = max(0.0, total_bruto - total_descuentos)

    return {
        "fecha": fecha,
        "docs": docs,
        "docs_anulados": docs_anulados,
        "total_bruto": round(total_bruto, 2),
        "total_descuentos": round(total_descuentos, 2),
        "total": round(total_neto, 2),
    }


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
    parser.add_argument(
        "--incluir-anulados",
        action="store_true",
        help="Suma también documentos anulados (default: excluye solo ANULADO)",
    )
    args = parser.parse_args()

    fecha = args.fecha or datetime.now().strftime("%Y-%m-%d")
    sin_iva = args.modo != "con_iva"

    res = calcular_total_smartmenu(
        fecha,
        desde_hora=args.desde,
        hasta_hora=args.hasta,
        sin_iva=sin_iva,
        incluir_anulados=args.incluir_anulados,
    )
    etiqueta = "SUBTOTAL sin IVA" if sin_iva else "TOTAL con IVA"
    rango = ""
    if args.desde or args.hasta:
        rango = f" [{args.desde or '00:00'}-{args.hasta or '23:59'}]"
    extra = ""
    if res.get("docs_anulados", 0) and not args.incluir_anulados:
        extra = f" | {res['docs_anulados']} doc(s) anulado(s) excluido(s) del total"
    desc_txt = ""
    if sin_iva and (res.get("total_descuentos") or 0) > 0:
        desc_txt = (
            f" | Brutas ${res['total_bruto']:.2f} - Desc. ${res['total_descuentos']:.2f} "
            f"= Netas ${res['total']:.2f}"
        )
    print(
        f"Smart Menu {etiqueta} {res['fecha']}{rango} ({res['docs']} docs): "
        f"${res['total']}{desc_txt}{extra}"
    )


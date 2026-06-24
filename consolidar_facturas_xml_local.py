import argparse
import os
from pathlib import Path
import re
import unicodedata

import gspread
from dotenv import load_dotenv
from codigo_factura_match import normalizar_cod_item_para_match
from procesar_facturas_drive import parsear_xml_sri
from google_credentials import google_credentials


load_dotenv(override=True)


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _sheet():
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _read_text(path: Path) -> str:
    # XML SRI suele venir en UTF-8, pero a veces tiene caracteres raros
    return path.read_text(encoding="utf-8", errors="replace")


def _as_text(v) -> str:
    """
    Fuerza representación como texto para que Google Sheets no elimine ceros a la izquierda
    (p.ej. RUC 019...).
    """
    s = "" if v is None else str(v).strip()
    if not s:
        return ""
    # Prefijo apostrofe = texto literal en Sheets
    return "'" + s


def _ensure_worksheet(sh, title: str):
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=5000, cols=30)


def _norm(s: str) -> str:
    s = (s or "").strip().upper()
    s = "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def main():
    p = argparse.ArgumentParser(
        description="Consolida XML SRI locales a una hoja (una fila por item)."
    )
    p.add_argument("--dir", required=True, help="Directorio con XMLs")
    p.add_argument(
        "--sheet-raw",
        default="FACTURAS_CONSOLIDADO_ITEMS_RAW",
        help="Hoja destino (detalle raw, una fila por item)",
    )
    p.add_argument(
        "--sheet",
        default="FACTURAS_CONSOLIDADO_ITEMS",
        help="Hoja destino (items únicos - dedupe)",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="Buscar XMLs en subcarpetas",
    )
    args = p.parse_args()

    base = Path(args.dir).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        raise SystemExit(f"Directorio no existe: {base}")

    xml_paths = (
        sorted(base.rglob("*.xml")) if args.recursive else sorted(base.glob("*.xml"))
    )
    print(f"XMLs encontrados: {len(xml_paths)} en {base}")
    if not xml_paths:
        return

    sh = _sheet()
    ws_raw = _ensure_worksheet(sh, args.sheet_raw)
    ws_uni = _ensure_worksheet(sh, args.sheet)

    headers = [
        "archivo_xml",
        "fecha_factura",
        "ruc",
        "razon_social",
        "num_factura",
        "num_autorizacion",
        "total_sin_impuesto_factura",
        "cod_item_xml",
        "descripcion_proveedor",
        "cantidad",
        "precio_unitario_xml",
        "descuento",
        "precio_total_sin_impuesto_item",
        "costo_efectivo",
    ]

    rows_out: list[list] = []
    xmls_parseados = 0
    items_total = 0
    xmls_error = 0

    for path in xml_paths:
        texto = _read_text(path)
        factura = parsear_xml_sri(texto)
        if not factura:
            xmls_error += 1
            continue

        xmls_parseados += 1
        for it in factura.get("items", []):
            items_total += 1
            rows_out.append(
                [
                    path.name,
                    factura.get("fecha_factura", ""),
                    _as_text(factura.get("ruc", "")),
                    factura.get("razon_social", ""),
                    factura.get("num_factura", ""),
                    factura.get("num_autorizacion", ""),
                    factura.get("total_sin_impuesto", 0),
                    _as_text(it.get("cod_item_xml", "")),
                    it.get("descripcion_proveedor", ""),
                    it.get("cantidad", 0),
                    it.get("precio_unitario_xml", 0),
                    it.get("descuento", 0),
                    it.get("precio_total_sin_impuesto", 0),
                    it.get("costo_efectivo", 0),
                ]
            )

    print(f"XMLs parseados OK: {xmls_parseados} | errores: {xmls_error}")
    print(f"Items consolidados: {items_total}")

    # 1) RAW (todas las líneas)
    ws_raw.clear()
    ws_raw.update(range_name="A1", values=[headers])

    if rows_out:
        batch = 500
        for i in range(0, len(rows_out), batch):
            chunk = rows_out[i : i + batch]
            # RAW para no convertir textos a números (preserva ceros a la izquierda)
            ws_raw.append_rows(chunk, value_input_option="RAW")
            print(f"  subidos: {min(i+batch, len(rows_out))}/{len(rows_out)} filas")

    # 2) DEDUPE: 1 registro por "producto proveedor"
    # Clave: (ruc, cod normalizado p/match, p.ej. COLEMUN sin -orden) si existe; sino (ruc, desc_norm)
    uniques: dict[tuple[str, str], list] = {}
    for r in rows_out:
        ruc = _norm(str(r[2]))
        ruc_raw = str(r[2]).strip().lstrip("'")
        cod_raw = str(r[7]).strip().lstrip("'")
        razon = str(r[3] or "")
        cod = normalizar_cod_item_para_match(cod_raw, razon, ruc_raw)
        desc = _norm(str(r[8]))
        key = (ruc, cod) if cod else (ruc, desc)
        if key in uniques:
            continue
        uniques[key] = r

    rows_uni = list(uniques.values())
    print(f"Items únicos (dedupe): {len(rows_uni)}")

    ws_uni.clear()
    ws_uni.update(range_name="A1", values=[headers])
    if rows_uni:
        batch = 500
        for i in range(0, len(rows_uni), batch):
            chunk = rows_uni[i : i + batch]
            ws_uni.append_rows(chunk, value_input_option="RAW")

    print("Listo. Hojas actualizadas:", args.sheet_raw, "y", args.sheet)


if __name__ == "__main__":
    main()


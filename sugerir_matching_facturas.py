import os
import re
import unicodedata
from difflib import SequenceMatcher

import gspread
from dotenv import load_dotenv
from codigo_factura_match import (
    cod_proveedores_strip_sufijo_desde_bd_prov,
    normalizar_cod_item_para_match,
)
from google_credentials import google_credentials


load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _sheet():
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _norm(s: str) -> str:
    s = (s or "").strip().upper()
    s = "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_ruc(s: str) -> str:
    """
    Normaliza RUC a 13 dígitos si viene numérico sin ceros a la izquierda.
    """
    s = (s or "").strip().lstrip("'")
    digits = re.sub(r"\D+", "", s)
    if not digits:
        return ""
    if len(digits) < 13:
        digits = digits.zfill(13)
    return digits


def _sim(a: str, b: str) -> float:
    a = _norm(a)
    b = _norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _ensure_ws(sh, title: str):
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=5000, cols=40)


def cargar_lookup_ruc_a_cod_prov(sh) -> dict[str, str]:
    ws = sh.worksheet("BD_PROV")
    values = ws.get_all_values()
    header_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_proveedor" for c in row):
            header_idx = i
            break
    if header_idx is None:
        return {}
    headers = [h.strip() for h in values[header_idx]]
    rows = values[header_idx + 1 :]

    def idx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    i_cod = idx("cod_proveedor")
    i_ruc = idx("RUC")
    if i_cod is None or i_ruc is None:
        return {}

    out = {}
    for r in rows:
        if len(r) <= max(i_cod, i_ruc):
            continue
        cod = (r[i_cod] or "").strip()
        ruc = (r[i_ruc] or "").strip()
        if cod and ruc and not cod.startswith("["):
            out[ruc] = cod
    return out


def cargar_items_prov(sh) -> list[dict]:
    ws = sh.worksheet("BD_ITEMS_PROV")
    values = ws.get_all_values()
    header_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_item_prov" for c in row):
            header_idx = i
            break
    if header_idx is None:
        return []
    headers = [h.strip() for h in values[header_idx]]
    rows = values[header_idx + 2 :]  # salta fila [FK][LINK][PK]...
    out = []
    for row in rows:
        if not any((c or "").strip() for c in row):
            continue
        d = {headers[i].strip(): (row[i] if i < len(row) else "").strip() for i in range(len(headers))}
        if d.get("activo", "SI").strip().upper() == "NO":
            continue
        out.append(d)
    return out


def main():
    sh = _sheet()

    # Input sheet
    ws_in = sh.worksheet("FACTURAS_CONSOLIDADO_ITEMS")
    values = ws_in.get_all_values()
    if not values or len(values) < 2:
        print("No hay datos en FACTURAS_CONSOLIDADO_ITEMS")
        return
    headers = [h.strip() for h in values[0]]
    rows = values[1:]

    def hidx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    i_ruc = hidx("ruc")
    i_cod = hidx("cod_item_xml")
    i_desc = hidx("descripcion_proveedor")
    i_razon = hidx("razon_social")
    if i_ruc is None or i_cod is None or i_desc is None:
        raise SystemExit("Faltan columnas requeridas (ruc, cod_item_xml, descripcion_proveedor)")

    # Lookups
    print("Cargando BD_PROV (RUC -> cod_proveedor)...")
    ruc_to_codprov = cargar_lookup_ruc_a_cod_prov(sh)
    print(f"  {len(ruc_to_codprov)} proveedores en lookup")

    ws_prov = sh.worksheet("BD_PROV")
    strip_sufijo_prov = cod_proveedores_strip_sufijo_desde_bd_prov(ws_prov.get_all_values())
    if strip_sufijo_prov:
        print(f"  cod_proveedor con normalización de sufijo factura: {len(strip_sufijo_prov)}")

    print("Cargando BD_ITEMS_PROV...")
    items = cargar_items_prov(sh)
    print(f"  {len(items)} items activos")

    # Index items by proveedor+codigo
    by_prov_code: dict[tuple[str, str], dict] = {}
    by_prov: dict[str, list[dict]] = {}
    for it in items:
        cod_prov = (it.get("cod_proveedor") or "").strip()
        cod_item = normalizar_cod_item_para_match(
            it.get("cod_item_prov") or "",
            "",
            "",
            cod_proveedor=cod_prov,
            cod_proveedores_strip=strip_sufijo_prov,
        )
        if cod_prov and cod_item:
            by_prov_code[(cod_prov, cod_item)] = it
        if cod_prov:
            by_prov.setdefault(cod_prov, []).append(it)

    out_headers = headers + [
        "cod_proveedor",
        "cod_mp_sistema_sugerido",
        "score",
        "estado_match",
        "metodo_match",
        "descripcion_item_prov_match",
        "cod_item_prov_match",
    ]

    out_rows: list[list] = []
    ok = revisar = sin_prov = 0

    for row in rows:
        ruc = _norm_ruc((row[i_ruc] if i_ruc < len(row) else ""))
        cod_xml = (row[i_cod] if i_cod < len(row) else "").strip()
        desc_xml = (row[i_desc] if i_desc < len(row) else "").strip()
        razon = (
            (row[i_razon] if i_razon is not None and i_razon < len(row) else "").strip()
        )

        cod_prov = ruc_to_codprov.get(ruc, "")
        sug_cod_mp = ""
        score = 0.0
        estado = "REVISAR"
        metodo = ""
        desc_match = ""
        cod_item_match = ""

        if not cod_prov:
            sin_prov += 1
            estado = "REVISAR"
            metodo = "SIN_PROVEEDOR"
        else:
            # 1) exact por codigo
            cod_norm = normalizar_cod_item_para_match(
                cod_xml,
                razon,
                ruc,
                cod_proveedor=cod_prov,
                cod_proveedores_strip=strip_sufijo_prov,
            )
            it = by_prov_code.get((cod_prov, cod_norm)) if cod_norm else None
            if it:
                sug_cod_mp = (it.get("cod_mp_sistema") or "").strip()
                score = 1.0
                estado = "OK" if sug_cod_mp else "REVISAR"
                metodo = "CODIGO_EXACTO"
                desc_match = it.get("descripcion_proveedor", "")
                cod_item_match = it.get("cod_item_prov", "")
            else:
                # 2) fuzzy por descripcion dentro del proveedor
                candidatos = by_prov.get(cod_prov, [])
                best = None
                best_s = 0.0
                for cand in candidatos:
                    s = _sim(desc_xml, cand.get("descripcion_proveedor", ""))
                    if s > best_s:
                        best_s = s
                        best = cand
                if best:
                    sug_cod_mp = (best.get("cod_mp_sistema") or "").strip()
                    score = round(best_s, 4)
                    metodo = "FUZZY_DESC"
                    estado = "OK" if (score >= 0.86 and sug_cod_mp) else "REVISAR"
                    desc_match = best.get("descripcion_proveedor", "")
                    cod_item_match = best.get("cod_item_prov", "")

        if estado == "OK":
            ok += 1
        else:
            revisar += 1

        out_rows.append(
            row
            + [
                cod_prov,
                sug_cod_mp,
                score,
                estado,
                metodo,
                desc_match,
                cod_item_match,
            ]
        )

    print(f"Sugerencias: OK={ok} | REVISAR={revisar} | sin proveedor={sin_prov}")

    ws_out = _ensure_ws(sh, "FACTURAS_MATCH_SUGERENCIAS")
    ws_out.clear()
    ws_out.update(range_name="A1", values=[out_headers])
    if out_rows:
        batch = 500
        for i in range(0, len(out_rows), batch):
            ws_out.append_rows(out_rows[i : i + batch], value_input_option="USER_ENTERED")
            print(f"  subidos: {min(i+batch, len(out_rows))}/{len(out_rows)}")

    print("Listo. Revisa la hoja: FACTURAS_MATCH_SUGERENCIAS")


if __name__ == "__main__":
    main()


import os

import gspread
from dotenv import load_dotenv
from google_credentials import google_credentials
load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_sheet():
    creds = google_credentials(SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.getenv("SPREADSHEET_ID"))


def _fila_encabezados_bd_productos(values: list[list[str]]) -> int:
    """Detecta la fila de headers (cod_smart_menu). Soporta layout antiguo y actual."""
    for i, row in enumerate(values[:8]):
        if any((c or "").strip().lower() == "cod_smart_menu" for c in row):
            return i
    # Legacy: fila 3 en Sheets (index 2)
    return 2 if len(values) > 2 else 0


# ── CARGA BD_PRODUCTOS ───────────────────────────────────────
def cargar_bd_productos() -> list[dict]:
    sh = _get_sheet()
    ws = sh.worksheet("BD_PRODUCTOS")
    values = ws.get_all_values()
    hi = _fila_encabezados_bd_productos(values)
    headers = values[hi]
    rows = values[hi + 1 :]
    result = []
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        r = {
            headers[i].strip(): row[i].strip()
            for i in range(min(len(headers), len(row)))
            if headers[i].strip()
        }
        if r.get("activo", "SI") == "SI" and r.get("cod_smart_menu"):
            result.append(r)
    return result


def _cod_en_lookup(cod_smart_menu: str, lookup: dict) -> str | None:
    """Resuelve cod POS (p. ej. 01010122) al cod_smart_menu del catálogo."""
    cod = str(cod_smart_menu).strip()
    if cod in lookup:
        return cod
    if cod.isdigit():
        alt = cod.lstrip("0") or "0"
        if alt in lookup:
            return alt
    return None


def acomp_lomo_kuro(detalle_plato: str | None) -> str | None:
    """arroz | papas si el POS lo indica (incluye texto tras OBS:)."""
    u = (detalle_plato or "").upper()
    if "ARROZ" in u:
        return "arroz"
    if "PAPA" in u:
        return "papas"
    return None


# ── CONSTRUYE TABLA DE LOOKUP ─────────────────────────────────
def construir_lookup(productos: list[dict]) -> dict:
    """
    Retorna dict:
    {
      "5": {
        "variedades": ["CAMARON AL WOK", "LOMO PONZU", "CRISPY (POLLO)", ...],
        "nombre_producto": "BAO",
        "filas": [...]
      },
      ...
    }
    """
    lookup: dict[str, dict] = {}
    for p in productos:
        cod = p.get("cod_smart_menu", "").strip()
        if not cod:
            continue
        if cod not in lookup:
            lookup[cod] = {
                "nombre_producto": p.get("nombre_producto", ""),
                "variedades": [],
                "filas": [],
            }
        variedad = p.get("variedad_smart_menu", "").strip()
        lookup[cod]["variedades"].append(variedad)
        lookup[cod]["filas"].append(p)
    # Alias sin ceros a la izquierda (POS manda 01010122, catálogo 1010122).
    for cod in list(lookup.keys()):
        if cod.isdigit():
            alt = cod.lstrip("0") or "0"
            if alt != cod and alt not in lookup:
                lookup[alt] = lookup[cod]
    return lookup


# ── MATCHING: detallePlato → cod_receta + variedad ────────────
def resolver_match(cod_smart_menu: str, detalle_plato: str, lookup: dict) -> dict:
    """
    Retorna:
    {
      "cod_producto": "...",
      "nombre_producto": "...",
      "variedad_matched": "...",
      "cod_receta": "...",
      "estado_match": "PROCESADO" | "PENDIENTE_MATCH"
    }
    """
    cod = str(cod_smart_menu).strip()
    detalle = (detalle_plato or "").strip().upper()

    cod_key = _cod_en_lookup(cod, lookup)
    if not cod_key:
        return _sin_match()

    entrada = lookup[cod_key]
    variedades = entrada["variedades"]
    filas = entrada["filas"]

    # Caso 1: una sola fila (sin variedad) → match directo
    if len(filas) == 1:
        f = filas[0]
        return _match_ok(f)

    # Caso 2: múltiples variedades → buscar cuál está en detalle_plato
    # Priorizar extras explícitos (CAMARON, LOMO…) antes que la fila clásica (variedad vacía).
    orden = sorted(
        range(len(filas)),
        key=lambda i: (0 if (variedades[i] or "").strip() else 1, -len(variedades[i] or "")),
    )
    for i in orden:
        variedad = variedades[i]
        if variedad and variedad.upper() in detalle:
            return _match_ok(filas[i])

    # Lomo Kuro (sm 12): ARROZ/PAPAS pueden ir en OBS ("OBS: t.m arroz").
    acomp = acomp_lomo_kuro(detalle_plato)
    if acomp:
        for i, variedad in enumerate(variedades):
            if (variedad or "").strip().lower() == acomp:
                return _match_ok(filas[i])

    # Caso 3: sin extra en detalle → clásico
    # (variedad vacía histórica, o "clasico"/"clásico" tras variedades de sabores)
    for i, variedad in enumerate(variedades):
        if not (variedad or "").strip():
            return _match_ok(filas[i])
    for i, variedad in enumerate(variedades):
        if (variedad or "").strip().lower() in ("clasico", "clásico", "classic"):
            return _match_ok(filas[i])

    # Lomo Kuro sin acompañante explícito → papas (comportamiento histórico pre-variedades).
    if cod_key == "12" and len(filas) > 1:
        for i, variedad in enumerate(variedades):
            if (variedad or "").strip().lower() == "papas":
                return _match_ok(filas[i])

    return _sin_match(f"No match variedad para cod={cod} detalle='{detalle_plato}'")


def _match_ok(fila: dict) -> dict:
    return {
        "cod_producto": fila.get("cod_smart_menu", ""),
        "nombre_producto": fila.get("nombre_producto", ""),
        "variedad_matched": fila.get("variedad_smart_menu", ""),
        "cod_receta": fila.get("cod_receta", ""),
        "estado_match": "PROCESADO",
    }


def _sin_match(msg: str = "") -> dict:
    if msg:
        print(f"  WARN match: {msg}")
    return {
        "cod_producto": None,
        "nombre_producto": None,
        "variedad_matched": None,
        "cod_receta": None,
        "estado_match": "PENDIENTE_MATCH",
    }


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Cargando BD_PRODUCTOS...")
    productos = cargar_bd_productos()
    print(f"  {len(productos)} productos activos cargados")

    lookup = construir_lookup(productos)
    print(f"  {len(lookup)} cod_smart_menu unicos en lookup")

    # Prueba con los 3 casos reales de Smart Menu
    casos = [
        ("5", "BAO CRISPY (POLLO)"),
        ("1", "DUMPLINGS (5 UNID) CERDO"),
        ("30", "LIMONADA    OBS: sin azucar con stevia"),
    ]

    print("\nTest de matching:")
    for cod, detalle in casos:
        resultado = resolver_match(cod, detalle, lookup)
        print(f"  cod={cod} | detalle='{detalle}'")
        print(f"    -> {resultado}")


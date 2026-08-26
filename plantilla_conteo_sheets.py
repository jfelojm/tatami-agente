"""
Crea (o rellena) la pestaña CONTEO en Google Sheets para pruebas de inventario físico.

Layout fijo (debe coincidir con scripts_apps_script/conteo_exportar_envio.gs
y tatami_maestro_unificado.gs):
  Fila 1: título
  Fila 2: ciclo_id | (valor / pegar UUID)
  Fila 3: enviado_por |
  Fila 4: enviado_por_contacto |
  Fila 5: observaciones |
  Fila 6: cabeceras de tabla
  Fila 7+: datos (rellenar columna conteo_fisico = H)
  Filas sin cod_mp_sistema = separadores de categoría (Apps Script las omite).

Uso:
  python plantilla_conteo_sheets.py --dry-run
  python plantilla_conteo_sheets.py --produccion
  python plantilla_conteo_sheets.py --produccion --desde-ciclo-id <uuid>
  python plantilla_conteo_sheets.py --produccion --nombre-hoja CONTEO_PRUEBA

Requiere .env: GOOGLE_CREDENTIALS_JSON o GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID; para --desde-ciclo-id también Supabase.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
import gspread
from supabase import create_client
from google_credentials import google_credentials, has_google_credentials

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

META_ROWS = 6  # filas 1..6 antes de datos; fila 6 = headers
HEADER_ROW = 6
DATA_START = 7
N_COLS = 9  # A..I
COL_RANGE = "A1:I"

HEADERS = [
    "line_no",
    "cod_mp_sistema",
    "cod_bodega",
    "categoria",
    "nombre_mp",
    "unidad_base",
    "stock_sistema_snapshot",
    "conteo_fisico",
    "notas",
]

COL_CONTEO_LETRA = "H"


def _sb():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


def _paginar_lineas_ciclo(sb, ciclo_id: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    # categoria puede no existir aún en BD — pedirla y degradar si falla
    select_con_cat = (
        "line_no,cod_mp_sistema,cod_bodega,categoria,nombre_mp,unidad_base,"
        "stock_sistema_snapshot,costo_unitario_ref_snapshot,conteo_fisico,notas"
    )
    select_sin_cat = (
        "line_no,cod_mp_sistema,cod_bodega,nombre_mp,unidad_base,"
        "stock_sistema_snapshot,costo_unitario_ref_snapshot,conteo_fisico,notas"
    )
    select = select_con_cat
    while True:
        try:
            chunk = (
                sb.table("conteo_linea")
                .select(select)
                .eq("ciclo_id", ciclo_id)
                .order("line_no")
                .range(offset, offset + 999)
                .execute()
                .data
                or []
            )
        except Exception:
            if select == select_con_cat:
                select = select_sin_cat
                continue
            raise
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def _open_spreadsheet():
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _armar_meta_block(ciclo_id: str | None) -> list[list[str]]:
    """Filas 1-5 + fila 6 headers en una sola matriz para update A1:I6."""
    title = (
        "TATAMI — Conteo físico | Rellenar columna conteo_fisico (H). "
        "Filas de categoría (sin código) no se envían. "
        "0 es válido. Exportar: menú Conteo / Apps Script."
    )
    empty = [""] * (N_COLS - 1)
    r1 = [title, *empty]
    r2 = ["ciclo_id (UUID)", ciclo_id or "", *([""] * (N_COLS - 2))]
    r3 = ["enviado_por", "", *([""] * (N_COLS - 2))]
    r4 = ["enviado_por_contacto", "", *([""] * (N_COLS - 2))]
    r5 = ["observaciones", "", *([""] * (N_COLS - 2))]
    r6 = HEADERS
    return [r1, r2, r3, r4, r5, r6]


def enriquecer_lineas_con_categoria(lineas: list[dict]) -> list[dict]:
    """Completa categoria desde maestro si falta; ordena por categoría + nombre."""
    from conteo_fisico import (
        _clave_orden_categoria,
        _mapa_categoria_maestro,
        normalizar_categoria_conteo,
    )

    if not lineas:
        return []
    bods = {(L.get("cod_bodega") or "").strip() for L in lineas}
    bods.discard("")
    mapa: dict[tuple[str, str], str] = {}
    if len(bods) == 1:
        mapa = _mapa_categoria_maestro(next(iter(bods)))
    else:
        mapa = _mapa_categoria_maestro(None)

    out: list[dict] = []
    for L in lineas:
        row = dict(L)
        cat = (row.get("categoria") or "").strip()
        if not cat:
            key = (
                (row.get("cod_mp_sistema") or "").strip(),
                (row.get("cod_bodega") or "").strip(),
            )
            cat = mapa.get(key) or normalizar_categoria_conteo(None)
        row["categoria"] = normalizar_categoria_conteo(cat)
        out.append(row)
    out.sort(
        key=lambda r: _clave_orden_categoria(
            r.get("categoria"), r.get("nombre_mp") or ""
        )
    )
    return out


def _fila_seccion(categoria: str) -> list:
    """Fila separadora: sin cod_mp (Apps Script la omite al enviar)."""
    titulo = f"—— {categoria} ——"
    return ["", "", "", categoria, titulo, "", "", "", ""]


def _lineas_a_filas(lineas: list[dict], *, con_secciones: bool = True) -> list[list]:
    """Convierte líneas DB a filas Sheets; opcionalmente inserta separadores por categoría."""
    enriquecidas = enriquecer_lineas_con_categoria(lineas)
    out: list[list] = []
    cat_prev: str | None = None
    for L in enriquecidas:
        cat = (L.get("categoria") or "").strip() or "Sin categoría"
        if con_secciones and cat != cat_prev:
            out.append(_fila_seccion(cat))
            cat_prev = cat
        stock = L.get("stock_sistema_snapshot")
        cf = L.get("conteo_fisico")
        out.append(
            [
                L.get("line_no") if L.get("line_no") is not None else "",
                (L.get("cod_mp_sistema") or "").strip(),
                (L.get("cod_bodega") or "").strip(),
                cat,
                (L.get("nombre_mp") or "").strip(),
                (L.get("unidad_base") or "").strip(),
                stock if stock is not None else "",
                cf if cf is not None else "",
                (L.get("notas") or "").strip(),
            ]
        )
    return out


def _leer_conteos_hoja_existente(ws) -> dict[str, float]:
    """cod_mp → conteo_fisico desde hoja actual (soporta layout viejo G o nuevo H)."""
    from sheet_numbers import parse_sheet_number

    vals = ws.get_all_values()
    if len(vals) < HEADER_ROW:
        return {}
    headers = [(c or "").strip().lower() for c in vals[HEADER_ROW - 1]]
    try:
        i_cod = headers.index("cod_mp_sistema")
    except ValueError:
        i_cod = 1
    try:
        i_cf = headers.index("conteo_fisico")
    except ValueError:
        i_cf = 6 if len(headers) <= 8 else 7  # G viejo / H nuevo
    out: dict[str, float] = {}
    for row in vals[DATA_START - 1 :]:
        if i_cod >= len(row):
            continue
        cod = (row[i_cod] or "").strip()
        if not cod:
            continue
        raw = row[i_cf] if i_cf < len(row) else ""
        if raw is None or str(raw).strip() == "":
            continue
        out[cod] = parse_sheet_number(raw, 0.0)
    return out


def generar_plantilla_desde_ciclo(
    ciclo_id: str,
    nombre_hoja: str | None = None,
    *,
    sobreescribir: bool = False,
    preservar_conteos_hoja: bool = True,
) -> dict:
    """
    Crea pestaña de conteo en SPREADSHEET_ID y rellena filas desde conteo_linea.
    Retorna resumen para WhatsApp / API.
    """
    ciclo_id = (ciclo_id or "").strip()
    if not ciclo_id:
        raise ValueError("ciclo_id requerido")

    sb = _sb()
    if not sb:
        raise RuntimeError("Falta SUPABASE_URL / SUPABASE_KEY")

    r = sb.table("conteo_ciclo").select("sheet_name,spreadsheet_id,cod_bodega").eq("id", ciclo_id).limit(1).execute()
    ciclo = (r.data or [None])[0]
    if not ciclo:
        raise ValueError(f"No existe ciclo {ciclo_id}")

    nombre = (nombre_hoja or ciclo.get("sheet_name") or "CONTEO").strip() or "CONTEO"
    lineas_db = _paginar_lineas_ciclo(sb, ciclo_id)
    if not lineas_db:
        raise ValueError(
            f"Sin filas en conteo_linea para {ciclo_id}. Ejecute snapshot antes de la plantilla."
        )

    if not has_google_credentials() or not os.getenv("SPREADSHEET_ID"):
        raise RuntimeError("Credenciales Google y SPREADSHEET_ID requeridos")

    sh = _open_spreadsheet()
    conteos_hoja: dict[str, float] = {}
    try:
        existing = sh.worksheet(nombre)
        if preservar_conteos_hoja:
            conteos_hoja = _leer_conteos_hoja_existente(existing)
        if sobreescribir:
            sh.del_worksheet(existing)
        else:
            raise ValueError(
                f"Ya existe la pestaña '{nombre}'. Use sobreescribir=True o otro nombre."
            )
    except gspread.exceptions.WorksheetNotFound:
        pass

    if conteos_hoja:
        for L in lineas_db:
            cod = (L.get("cod_mp_sistema") or "").strip()
            if cod in conteos_hoja and L.get("conteo_fisico") is None:
                L["conteo_fisico"] = conteos_hoja[cod]

    filas = _lineas_a_filas(lineas_db, con_secciones=True)
    nrows = max(500, len(filas) + DATA_START + 50)
    ws = sh.add_worksheet(title=nombre, rows=nrows, cols=max(12, N_COLS))
    meta = _armar_meta_block(ciclo_id)
    ws.update(range_name=f"A1:I{META_ROWS}", values=meta, value_input_option="USER_ENTERED")
    end = DATA_START + len(filas) - 1
    ws.update(range_name=f"A{DATA_START}:I{end}", values=filas, value_input_option="USER_ENTERED")
    try:
        ws.freeze(rows=META_ROWS)
    except Exception:
        pass
    try:
        # Formato texto en cod_mp (B) para ceros a la izquierda
        ws.format(f"B{DATA_START}:B{end}", {"numberFormat": {"type": "TEXT"}})
    except Exception:
        pass

    sid = os.getenv("SPREADSHEET_ID", "")
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={ws.id}"
    n_mp = sum(1 for f in filas if f[1])
    n_sec = len(filas) - n_mp
    return {
        "ciclo_id": ciclo_id,
        "nombre_hoja": nombre,
        "spreadsheet_id": sid,
        "url_hoja": url,
        "filas_datos": n_mp,
        "filas_seccion": n_sec,
        "fila_inicio_datos": DATA_START,
        "columna_conteo": COL_CONTEO_LETRA,
        "conteos_preservados_hoja": len(conteos_hoja),
    }


def regenerar_plantillas_ciclos_abiertos(*, dry_run: bool = False) -> list[dict]:
    """Regenera hojas de ciclos no cerrados con categorías (preserva conteos en hoja/DB)."""
    from conteo_fisico import listar_ciclos_api

    abiertos = [
        c
        for c in listar_ciclos_api(limit=50)
        if (c.get("estado") or "") not in ("CONTABILIZADO", "ANULADO")
    ]
    resultados: list[dict] = []
    for c in abiertos:
        cid = (c.get("id") or "").strip()
        hoja = (c.get("sheet_name") or "CONTEO").strip()
        info = {
            "ciclo_id": cid,
            "estado": c.get("estado"),
            "cod_bodega": c.get("cod_bodega"),
            "sheet_name": hoja,
        }
        if dry_run:
            info["accion"] = "dry_run"
            resultados.append(info)
            continue
        try:
            plantilla = generar_plantilla_desde_ciclo(
                cid, nombre_hoja=hoja, sobreescribir=True, preservar_conteos_hoja=True
            )
            info["accion"] = "ok"
            info["filas_datos"] = plantilla.get("filas_datos")
            info["filas_seccion"] = plantilla.get("filas_seccion")
            info["url_hoja"] = plantilla.get("url_hoja")
        except Exception as e:
            info["accion"] = "error"
            info["error"] = str(e)
        resultados.append(info)
    return resultados


def main() -> None:
    p = argparse.ArgumentParser(description="Plantilla Google Sheets para conteo físico")
    p.add_argument(
        "--nombre-hoja",
        default="CONTEO",
        help="Nombre de la pestaña (default CONTEO; igual que conteo_ciclo.sheet_name)",
    )
    p.add_argument(
        "--desde-ciclo-id",
        default="",
        help="Tras snapshot: rellena filas desde Supabase conteo_linea",
    )
    p.add_argument(
        "--sobreescribir",
        action="store_true",
        help="Si la pestaña existe, la borra y vuelve a crear",
    )
    p.add_argument(
        "--regenerar-abiertos",
        action="store_true",
        help="Regenera plantillas de ciclos abiertos con categorías",
    )
    p.add_argument("--produccion", action="store_true", help="Sin esto: solo muestra plan")
    args = p.parse_args()

    if args.regenerar_abiertos:
        res = regenerar_plantillas_ciclos_abiertos(dry_run=not args.produccion)
        for r in res:
            print(r)
        print(f"Total: {len(res)}")
        return

    nombre = args.nombre_hoja.strip() or "CONTEO"
    ciclo_id = (args.desde_ciclo_id or "").strip()

    lineas_db: list[dict] = []
    if ciclo_id:
        sb = _sb()
        if not sb:
            print("ERROR: falta SUPABASE_URL / SUPABASE_KEY para --desde-ciclo-id")
            raise SystemExit(1)
        lineas_db = _paginar_lineas_ciclo(sb, ciclo_id)
        if not lineas_db:
            print(f"WARN: no hay filas en conteo_linea para ciclo_id={ciclo_id} (¿snapshot ejecutado?)")

    if not args.produccion:
        print("[DRY RUN] Crearía pestaña:", nombre)
        print(f"  Meta + cabeceras: filas 1-{META_ROWS}, datos desde fila {DATA_START}")
        print(f"  Columnas A-I; conteo_fisico = {COL_CONTEO_LETRA}; categoría = D + secciones")
        if lineas_db:
            filas = _lineas_a_filas(lineas_db)
            n_mp = sum(1 for f in filas if f[1])
            print(f"  Insertaría {n_mp} MPs + {len(filas) - n_mp} filas sección")
        print("  Ejecutar con --produccion para escribir en Sheets.")
        return

    if ciclo_id:
        out = generar_plantilla_desde_ciclo(
            ciclo_id,
            nombre_hoja=nombre,
            sobreescribir=args.sobreescribir,
        )
        print("\nOK:", out)
        return

    if not has_google_credentials() or not os.getenv("SPREADSHEET_ID"):
        print("ERROR: credenciales Google y SPREADSHEET_ID requeridos")
        raise SystemExit(1)

    sh = _open_spreadsheet()

    try:
        existing = sh.worksheet(nombre)
        if args.sobreescribir:
            sh.del_worksheet(existing)
            print(f"  Pestaña anterior '{nombre}' eliminada (--sobreescribir).")
        else:
            print(
                f"ERROR: ya existe la pestaña '{nombre}'. "
                f"Use --sobreescribir para reemplazarla o --nombre-hoja OTRO."
            )
            raise SystemExit(1)
    except gspread.exceptions.WorksheetNotFound:
        pass

    nrows = max(500, len(lineas_db) + DATA_START + 50)
    ws = sh.add_worksheet(title=nombre, rows=nrows, cols=max(12, N_COLS))

    meta = _armar_meta_block(ciclo_id if ciclo_id else None)
    ws.update(range_name=f"A1:I{META_ROWS}", values=meta, value_input_option="USER_ENTERED")

    if lineas_db:
        filas = _lineas_a_filas(lineas_db)
        end = DATA_START + len(filas) - 1
        ws.update(range_name=f"A{DATA_START}:I{end}", values=filas, value_input_option="USER_ENTERED")

    try:
        ws.freeze(rows=META_ROWS)
    except Exception as e:
        print(f"  WARN: no se pudo congelar filas: {e}")

    sid = os.getenv("SPREADSHEET_ID", "")
    print("\nOK: plantilla lista.")
    print(f"  Pestaña: {nombre}")
    print(f"  Datos: desde fila {DATA_START} ({len(lineas_db)} filas desde BD)")
    print(f"  Columna conteo: {COL_CONTEO_LETRA}")
    print(f"  Spreadsheet ID (para conteo_ciclo / JSON): {sid}")
    print(
        "\n  Siguiente: 1) Actualizar Apps Script (layout A-I, conteo en H)"
        "\n            2) Rellenar columna conteo_fisico (H) y Enviar a Tatami"
    )


if __name__ == "__main__":
    main()

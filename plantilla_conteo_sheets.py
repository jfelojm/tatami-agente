"""
Crea (o rellena) la pestaña CONTEO en Google Sheets para pruebas de inventario físico.

Layout fijo (debe coincidir con scripts_apps_script/conteo_exportar_envio.gs):
  Fila 1: título
  Fila 2: ciclo_id | (valor / pegar UUID)
  Fila 3: enviado_por |
  Fila 4: enviado_por_contacto |
  Fila 5: observaciones |
  Fila 6: cabeceras de tabla
  Fila 7+: datos (rellenar columna conteo_fisico)

Uso:
  python plantilla_conteo_sheets.py --dry-run
  python plantilla_conteo_sheets.py --produccion
  python plantilla_conteo_sheets.py --produccion --desde-ciclo-id <uuid>
  python plantilla_conteo_sheets.py --produccion --nombre-hoja CONTEO_PRUEBA

Requiere .env: GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID; para --desde-ciclo-id también Supabase.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

META_ROWS = 6  # filas 1..6 antes de datos; fila 6 = headers
HEADER_ROW = 6
DATA_START = 7

HEADERS = [
    "line_no",
    "cod_mp_sistema",
    "cod_bodega",
    "nombre_mp",
    "unidad_base",
    "stock_sistema_snapshot",
    "conteo_fisico",
    "notas",
]


def _sb():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


def _paginar_lineas_ciclo(sb, ciclo_id: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("conteo_linea")
            .select(
                "line_no,cod_mp_sistema,cod_bodega,nombre_mp,unidad_base,"
                "stock_sistema_snapshot,costo_unitario_ref_snapshot,conteo_fisico,notas"
            )
            .eq("ciclo_id", ciclo_id)
            .order("line_no")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def _open_spreadsheet():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _armar_meta_block(ciclo_id: str | None) -> list[list[str]]:
    """Filas 1-5 + fila 6 headers en una sola matriz para update A1:H6."""
    title = (
        "TATAMI — Conteo físico | Rellenar columna conteo_fisico (G). "
        "0 es válido. Exportar JSON: ver scripts_apps_script/conteo_exportar_envio.gs"
    )
    r1 = [title, "", "", "", "", "", "", ""]
    r2 = ["ciclo_id (UUID)", ciclo_id or "", "", "", "", "", "", ""]
    r3 = ["enviado_por", "", "", "", "", "", "", ""]
    r4 = ["enviado_por_contacto", "", "", "", "", "", "", ""]
    r5 = ["observaciones", "", "", "", "", "", "", ""]
    r6 = HEADERS
    return [r1, r2, r3, r4, r5, r6]


def _lineas_a_filas(lineas: list[dict]) -> list[list]:
    out: list[list] = []
    for L in lineas:
        stock = L.get("stock_sistema_snapshot")
        cf = L.get("conteo_fisico")
        out.append(
            [
                L.get("line_no") if L.get("line_no") is not None else "",
                (L.get("cod_mp_sistema") or "").strip(),
                (L.get("cod_bodega") or "").strip(),
                (L.get("nombre_mp") or "").strip(),
                (L.get("unidad_base") or "").strip(),
                stock if stock is not None else "",
                cf if cf is not None else "",
                (L.get("notas") or "").strip(),
            ]
        )
    return out


def generar_plantilla_desde_ciclo(
    ciclo_id: str,
    nombre_hoja: str | None = None,
    *,
    sobreescribir: bool = False,
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

    r = sb.table("conteo_ciclo").select("sheet_name,spreadsheet_id").eq("id", ciclo_id).limit(1).execute()
    ciclo = (r.data or [None])[0]
    if not ciclo:
        raise ValueError(f"No existe ciclo {ciclo_id}")

    nombre = (nombre_hoja or ciclo.get("sheet_name") or "CONTEO").strip() or "CONTEO"
    lineas_db = _paginar_lineas_ciclo(sb, ciclo_id)
    if not lineas_db:
        raise ValueError(
            f"Sin filas en conteo_linea para {ciclo_id}. Ejecute snapshot antes de la plantilla."
        )

    if not os.getenv("GOOGLE_CREDENTIALS_PATH") or not os.getenv("SPREADSHEET_ID"):
        raise RuntimeError("GOOGLE_CREDENTIALS_PATH y SPREADSHEET_ID requeridos")

    sh = _open_spreadsheet()
    try:
        existing = sh.worksheet(nombre)
        if sobreescribir:
            sh.del_worksheet(existing)
        else:
            raise ValueError(
                f"Ya existe la pestaña '{nombre}'. Use sobreescribir=True o otro nombre."
            )
    except gspread.exceptions.WorksheetNotFound:
        pass

    nrows = max(500, len(lineas_db) + DATA_START + 50)
    ws = sh.add_worksheet(title=nombre, rows=nrows, cols=10)
    meta = _armar_meta_block(ciclo_id)
    ws.update(f"A1:H{META_ROWS}", meta, value_input_option="USER_ENTERED")
    filas = _lineas_a_filas(lineas_db)
    end = DATA_START + len(filas) - 1
    ws.update(f"A{DATA_START}:H{end}", filas, value_input_option="USER_ENTERED")
    try:
        ws.freeze(rows=META_ROWS)
    except Exception:
        pass

    sid = os.getenv("SPREADSHEET_ID", "")
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={ws.id}"
    return {
        "ciclo_id": ciclo_id,
        "nombre_hoja": nombre,
        "spreadsheet_id": sid,
        "url_hoja": url,
        "filas_datos": len(lineas_db),
        "fila_inicio_datos": DATA_START,
        "columna_conteo": "G",
    }


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
    p.add_argument("--produccion", action="store_true", help="Sin esto: solo muestra plan")
    args = p.parse_args()

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
        if lineas_db:
            print(f"  Insertaría {len(lineas_db)} filas de datos desde Supabase")
        print("  Ejecutar con --produccion para escribir en Sheets.")
        return

    if not os.getenv("GOOGLE_CREDENTIALS_PATH") or not os.getenv("SPREADSHEET_ID"):
        print("ERROR: GOOGLE_CREDENTIALS_PATH y SPREADSHEET_ID requeridos")
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
    ws = sh.add_worksheet(title=nombre, rows=nrows, cols=10)

    meta = _armar_meta_block(ciclo_id if ciclo_id else None)
    ws.update(f"A1:H{META_ROWS}", meta, value_input_option="USER_ENTERED")

    if lineas_db:
        filas = _lineas_a_filas(lineas_db)
        end = DATA_START + len(filas) - 1
        ws.update(f"A{DATA_START}:H{end}", filas, value_input_option="USER_ENTERED")

    try:
        ws.freeze(rows=META_ROWS)
    except Exception as e:
        print(f"  WARN: no se pudo congelar filas: {e}")

    sid = os.getenv("SPREADSHEET_ID", "")
    print("\nOK: plantilla lista.")
    print(f"  Pestaña: {nombre}")
    print(f"  Datos: desde fila {DATA_START} ({len(lineas_db)} filas desde BD)")
    print(f"  Spreadsheet ID (para conteo_ciclo / JSON): {sid}")
    print(
        "\n  Siguiente: 1) Pegar Apps Script desde scripts_apps_script/conteo_exportar_envio.gs"
        "\n            2) Rellenar columna conteo_fisico y ejecutar exportarJsonConteo()"
        "\n            3) python conteo_fisico.py registrar-envio --ciclo-id ... --archivo payload.json --produccion"
    )


if __name__ == "__main__":
    main()

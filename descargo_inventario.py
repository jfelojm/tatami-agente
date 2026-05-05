import os
from datetime import datetime

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1
from supabase import create_client

from matching_productos import (
    cargar_bd_productos,
    construir_lookup,
    resolver_match,
)

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _iso_fecha_hora_mov(fecha_v: str | None, hora_raw: str | None) -> str:
    """ISO 8601 local para Postgres: evita duplicar ':00' si hora ya trae segundos."""
    fecha = (fecha_v or "").strip() or "1970-01-01"
    h = (hora_raw or "").strip()
    if not h:
        return f"{fecha}T00:00:00"
    parts = h.split(":")
    try:
        if len(parts) == 2:
            return f"{fecha}T{int(parts[0]):02d}:{int(parts[1]):02d}:00"
        if len(parts) >= 3:
            sec = parts[2].split(".")[0]
            return f"{fecha}T{int(parts[0]):02d}:{int(parts[1]):02d}:{int(sec or 0):02d}"
    except ValueError:
        pass
    return f"{fecha}T00:00:00"


def _limpiar_variedad(variedad: str | None) -> str:
    s = (variedad or "").strip().upper()
    if "OBS:" in s:
        s = s.split("OBS:", 1)[0].strip()
    return " ".join(s.split())


def _mismo_cod_receta(a: str, b: str) -> bool:
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    if a.isdigit() and b.isdigit():
        return int(a) == int(b)
    return False


# ── GOOGLE SHEETS ─────────────────────────────────────────────
def _get_sheet():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


# ── CARGA BD_RECETAS_DETALLE ──────────────────────────────────
_recetas_cache = None


def cargar_recetas() -> list[dict]:
    global _recetas_cache
    if _recetas_cache is not None:
        return _recetas_cache

    print("  Cargando BD_RECETAS_DETALLE...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    values = ws.get_all_values()
    headers = values[2]
    rows = values[4:]
    result = []
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        r = {
            headers[i].strip(): row[i].strip()
            for i in range(min(len(headers), len(row)))
        }
        cod_mp = r.get("cod_mp_sistema", "").strip()
        if not cod_mp or cod_mp.startswith("#"):
            continue
        result.append(r)

    print(f"  {len(result)} ingredientes cargados")
    _recetas_cache = result
    return result


def get_ingredientes(cod_receta: str, variedad: str | None) -> list[dict]:
    recetas = cargar_recetas()
    cod = cod_receta.strip()
    var = _limpiar_variedad(variedad)

    candidatas = [
        r for r in recetas if _mismo_cod_receta(r.get("cod_receta", ""), cod)
    ]
    if not candidatas:
        return []

    def filas_exactas(pool: list[dict], v: str) -> list[dict]:
        out = []
        for r in pool:
            r_var = _limpiar_variedad(r.get("variedad_smart_menu", ""))
            if v and r_var != v:
                continue
            if not v and r_var:
                continue
            out.append(r)
        return out

    resultado = filas_exactas(candidatas, var)
    if resultado:
        return resultado
    if var:
        sin_var = filas_exactas(candidatas, "")
        if sin_var:
            return sin_var
        fuzzy = []
        for r in candidatas:
            r_var = _limpiar_variedad(r.get("variedad_smart_menu", ""))
            if not r_var:
                continue
            if r_var in var or var in r_var:
                fuzzy.append(r)
        if fuzzy:
            return fuzzy
    return []


# ── CARGA BD_MP_SISTEMA ───────────────────────────────────────
_mp_cache = None
_mp_row_index: dict[str, int] = {}
_mp_stock_col_idx: int | None = None


def cargar_mp_sistema() -> dict:
    global _mp_cache, _mp_row_index, _mp_stock_col_idx

    if _mp_cache is not None:
        return _mp_cache

    print("  Cargando BD_MP_SISTEMA...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if any(c.strip() == "cod_mp_sistema" for c in row):
            header_row_idx = i
            break

    if header_row_idx is None:
        print("  ERROR: No se encontró header cod_mp_sistema en BD_MP_SISTEMA")
        return {}

    headers = [h.strip() for h in values[header_row_idx]]
    rows = values[header_row_idx + 1 :]

    try:
        _mp_stock_col_idx = headers.index("stock_actual") + 1
    except ValueError:
        _mp_stock_col_idx = None
        print("  WARN: columna stock_actual no encontrada en BD_MP_SISTEMA")

    result = {}
    _mp_row_index.clear()
    for i, row in enumerate(rows):
        if not any(c.strip() for c in row):
            continue
        r = {
            headers[j]: row[j].strip()
            for j in range(min(len(headers), len(row)))
        }
        cod = r.get("cod_mp_sistema", "").strip()
        if cod:
            result[cod] = r
            _mp_row_index[cod] = header_row_idx + i + 2

    print(f"  {len(result)} MPs cargados")
    _mp_cache = result
    return result


def actualizar_stocks_sheets_batch(cod_mp_a_stock: dict[str, float]):
    """Una sola apertura de libro + batch_update (evita 429 por lecturas repetidas)."""
    if not cod_mp_a_stock:
        return
    col_idx = _mp_stock_col_idx
    if not col_idx:
        print("  WARN: columna stock_actual desconocida, no se actualiza Sheets")
        return

    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    data: list[dict] = []
    for cod_mp, nuevo_stock in cod_mp_a_stock.items():
        row_idx = _mp_row_index.get(cod_mp)
        if not row_idx:
            print(f"  WARN: no se encontró fila para cod_mp={cod_mp}")
            continue
        rng = rowcol_to_a1(row_idx, col_idx)
        data.append({"range": rng, "values": [[round(float(nuevo_stock), 4)]]})

    if not data:
        return
    ws.batch_update(data)


_lookup_descargo = None


def _resolver_cod_receta(venta: dict) -> str | None:
    """
    Preferir columna cod_receta en hist_ventas; si falta (filas viejas),
    derivar desde cod_smart_menu + variedad vía BD_PRODUCTOS.
    Nota: cod_producto en hist_ventas suele ser el mismo valor que cod_smart_menu.
    """
    direct = (venta.get("cod_receta") or "").strip()
    if direct:
        return direct

    global _lookup_descargo
    if _lookup_descargo is None:
        _lookup_descargo = construir_lookup(cargar_bd_productos())

    cod_sm = (venta.get("cod_smart_menu") or "").strip()
    if not cod_sm:
        cod_sm = (venta.get("cod_producto") or "").strip()
    if not cod_sm:
        return None

    variedad = (venta.get("variedad_smart_menu") or "").strip()
    m = resolver_match(cod_sm, variedad, _lookup_descargo)
    return (m.get("cod_receta") or "").strip() or None


# ── CALCULAR CONSUMO ──────────────────────────────────────────
def calcular_consumo(ingrediente: dict, cantidad_vendida: float) -> float:
    try:
        gramaje = float(ingrediente.get("cantidad", 0))
        pct_aplicacion = float(ingrediente.get("pct_aplicacion", 1) or 1)
        merma_pct = float(ingrediente.get("merma_pct", 0) or 0)
    except ValueError:
        return 0.0

    return cantidad_vendida * gramaje * pct_aplicacion * (1 + merma_pct)


# ── PROCESAR DESCARGO ─────────────────────────────────────────
def procesar_descargo(fecha: str | None = None):
    query = (
        supabase.table("hist_ventas")
        .select("*")
        .eq("estado_match", "PROCESADO")
        .eq("descargado", False)
    )
    if fecha:
        query = query.eq("fecha", fecha)

    ventas = query.execute().data
    print(f"  {len(ventas)} ventas pendientes de descargo")

    if not ventas:
        return

    mp_sistema = cargar_mp_sistema()
    movimientos_ok = 0
    movimientos_err = 0
    stocks_actualizados: set[str] = set()

    for venta in ventas:
        cod_receta = _resolver_cod_receta(venta)
        variedad = venta.get("variedad_smart_menu")
        cantidad_v = float(venta.get("cantidad_vendida", 1))
        cod_venta = venta.get("cod_venta")
        fecha_v = venta.get("fecha")

        if not cod_receta:
            print(f"  WARN: venta {cod_venta} sin cod_receta, skip")
            continue

        ingredientes = get_ingredientes(cod_receta, variedad)
        if not ingredientes:
            print(
                f"  WARN: sin ingredientes para receta={cod_receta} variedad='{variedad}'"
            )
            continue

        movs: list[dict] = []
        deltas: list[tuple[str, float]] = []

        for ing in ingredientes:
            cod_mp = ing.get("cod_mp_sistema", "").strip()
            if not cod_mp or cod_mp.startswith("#"):
                continue

            consumo = calcular_consumo(ing, cantidad_v)
            if consumo <= 0:
                continue

            mp_info = mp_sistema.get(cod_mp, {})
            unidad = mp_info.get("unidad_base", "")
            bodega = mp_info.get("cod_bodega", "")
            costo_u = float(mp_info.get("costo_unitario_ref", 0) or 0)

            ts = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
            cod_mov = (
                f"MOV-{fecha_v.replace('-', '') if fecha_v else '00000000'}-{cod_mp}-{ts}"
            )

            mov = {
                "cod_mov": cod_mov,
                "fecha": _iso_fecha_hora_mov(fecha_v, venta.get("hora")),
                "tipo_mov": "SALIDA_VENTA",
                "cod_mp_sistema": cod_mp,
                "nombre_mp": ing.get("nombre_mp", ""),
                "cod_bodega_origen": bodega,
                "cod_bodega_destino": None,
                "cantidad_mov": round(consumo, 4),
                "unidad_base": unidad,
                "costo_unitario": costo_u,
                "costo_total": round(consumo * costo_u, 4),
                "origen_documento": "VENTA_SMART_MENU",
                "num_documento": cod_venta,
                "registrado_por": "AGENTE",
                "observaciones": (
                    f"Descargo automático receta {cod_receta} var={variedad}"
                ),
            }
            movs.append(mov)
            deltas.append((cod_mp, consumo))

        venta_ok = True
        if movs:
            try:
                supabase.table("mov_inventario").insert(movs).execute()
                movimientos_ok += len(movs)
            except Exception as e:
                print(f"  ERROR insertando movs venta {cod_venta}: {e}")
                movimientos_err += len(movs)
                venta_ok = False
        else:
            pass

        if venta_ok:
            for cod_mp, consumo in deltas:
                if cod_mp in mp_sistema:
                    stock_actual = float(mp_sistema[cod_mp].get("stock_actual") or 0)
                    mp_sistema[cod_mp]["stock_actual"] = stock_actual - consumo
                    stocks_actualizados.add(cod_mp)

            supabase.table("hist_ventas").update(
                {
                    "descargado": True,
                    "fecha_descargo": datetime.now().isoformat(),
                }
            ).eq("cod_venta", cod_venta).execute()

    print(f"  Actualizando {len(stocks_actualizados)} MPs en Sheets (batch)...")
    batch_stocks = {
        cod_mp: float(mp_sistema[cod_mp].get("stock_actual") or 0)
        for cod_mp in stocks_actualizados
    }
    try:
        actualizar_stocks_sheets_batch(batch_stocks)
    except Exception as e:
        print(f"  ERROR actualizando stock en Sheets: {e}")
        print("  (mov_inventario y hist_ventas ya quedaron actualizados en Supabase)")

    print(f"\n  Movimientos insertados: {movimientos_ok}")
    print(f"  Movimientos con error:  {movimientos_err}")
    print(f"  MPs actualizados:       {len(stocks_actualizados)}")


if __name__ == "__main__":
    fecha = (
        input("Fecha a descargar (YYYY-MM-DD) [Enter = todas pendientes]: ").strip()
        or None
    )

    print(f"\n{'=' * 50}")
    print(f"MODULO DESCARGO — {fecha or 'TODAS PENDIENTES'}")
    print(f"{'=' * 50}")

    print("\n[1] Cargando catálogos...")
    cargar_recetas()
    cargar_mp_sistema()

    print("\n[2] Procesando descargo...")
    procesar_descargo(fecha)

    print(f"\n{'=' * 50}")

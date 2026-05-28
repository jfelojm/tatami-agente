"""
Recalcula stock_actual en BD_MP_SISTEMA desde cero usando mov_inventario.

Stock por (cod_mp_sistema, cod_bodega) — una fila por par en la hoja.

Formula por (cod_mp, cod_bodega):
  + ENTRADA, AJUSTE_POSITIVO, TRASLADO_ENTRADA  (cod_bodega_destino)
  - SALIDA_VENTA, AJUSTE_NEGATIVO, TRASLADO_SALIDA (cod_bodega_origen)

Costo costo_unitario_ref (política canónica, ver costo_mp_canonico.py):
  1. Promedio ponderado de ENTRADAs en ventana (COSTO_REF_DIAS_VENTANA, default 90).
  2. Si BD_ITEMS_PROV tiene precio_ref: se usa prov salvo que el promedio mov sea coherente.
  3. Si el promedio mov parece pack sin dividir (>0.05 USD/gr), se corrige con factor del ítem.
  Evita que entradas históricas mal cargadas reinflen col/almidón y subrecetas.

CLI:
  python recalcular_stock_sheets.py --produccion
  python recalcular_stock_sheets.py --produccion --cod-mp 92
  python recalcular_stock_sheets.py --produccion --cod-bodega BOD-001
  python recalcular_stock_sheets.py --produccion --solo-costo
"""

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client
import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

from bodegas_config import normalizar_cod_bodega

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

TIPOS_SUMA_DESTINO = {"AJUSTE_POSITIVO", "ENTRADA", "TRASLADO_ENTRADA"}
TIPOS_RESTA_ORIGEN = {"SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRASLADO_SALIDA"}


def _cod_mp_norm(c: str) -> str:
    s = (c or "").strip()
    if not s:
        return ""
    n = s.lstrip("0")
    return n if n else "0"


def _clave_stock(cod_mp: str, cod_bodega: str) -> tuple[str, str]:
    return _cod_mp_norm(cod_mp), normalizar_cod_bodega(cod_bodega)


def _dias_ventana_costo() -> int:
    """0 = sin límite de fecha; default 90 días."""
    raw = (os.getenv("COSTO_REF_DIAS_VENTANA") or "90").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 90


def _parse_fecha_mov(fecha: str) -> datetime | None:
    s = (fecha or "").strip()[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fecha_dentro_ventana(fecha: str, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    dt = _parse_fecha_mov(fecha)
    if dt is None:
        return True
    return dt >= cutoff


def paginar_todo(tabla, select):
    rows = []
    offset = 0
    while True:
        chunk = supabase.table(tabla).select(select).range(offset, offset + 999).execute().data
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def _bodega_mov(m: dict, tipo: str) -> str:
    """Bodega afectada por el movimiento (destino en suma, origen en resta)."""
    if tipo in TIPOS_SUMA_DESTINO:
        return normalizar_cod_bodega(m.get("cod_bodega_destino") or m.get("cod_bodega_origen"))
    if tipo in TIPOS_RESTA_ORIGEN:
        return normalizar_cod_bodega(m.get("cod_bodega_origen") or m.get("cod_bodega_destino"))
    return ""


def _resolver_cod_mp_por_nombre_mp(
    values: list,
    header_row_idx: int,
    headers: list[str],
    col_cod: int,
    nombre_substr: str,
) -> str | None:
    try:
        inom = headers.index("nombre_mp")
    except ValueError:
        return None
    q = (nombre_substr or "").strip().upper()
    if not q:
        return None
    hits: list[str] = []
    for row in values[header_row_idx + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        nom = (row[inom] if inom < len(row) else "").strip().upper()
        codc = (row[col_cod] if col_cod < len(row) else "").strip()
        if not codc or not nom:
            continue
        if q in nom:
            hits.append(codc)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(f"  WARN: varios MPs coinciden con {nombre_substr!r}: {hits[:8]}...")
    return None


def _precio_unitario_desde_items_prov(precio: float, fac: float) -> float:
    from numeros_sheets import precio_ref_a_unidad_base

    return precio_ref_a_unidad_base(precio, fac)


def _cargar_costo_promedio_items_prov(sh) -> dict[str, float]:
    """
    cod_mp_norm -> promedio simple de precio unitario base (ítems prov activos).
    Varias filas mismo MP (proveedores distintos) contribuyen por igual.
    """
    ws = sh.worksheet("BD_ITEMS_PROV")
    values = ws.get_all_values()
    hi = None
    for i, r in enumerate(values):
        if any((c or "").strip() == "cod_mp_sistema" for c in r):
            hi = i
            break
    if hi is None:
        return {}
    headers = [(c or "").strip() for c in values[hi]]
    try:
        ic = headers.index("cod_mp_sistema")
        ip = headers.index("precio_ref")
        ifac = headers.index("factor_conversion")
    except ValueError:
        return {}
    try:
        ia = headers.index("activo")
    except ValueError:
        ia = None

    acum: dict[str, list[float]] = defaultdict(list)
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        if ia is not None and ia < len(row) and (row[ia] or "").strip().upper() == "NO":
            continue
        if ic >= len(row):
            continue
        c_mp = (row[ic] or "").strip()
        nk = _cod_mp_norm(c_mp)
        if not nk:
            continue
        pr = (row[ip] if ip < len(row) else "").strip()
        facs = (row[ifac] if ifac < len(row) else "").strip()
        if not pr or not facs:
            continue
        try:
            from numeros_sheets import parse_numero_sheets

            precio = parse_numero_sheets(pr)
            fac = parse_numero_sheets(facs, 1.0)
        except ValueError:
            continue
        if fac > 0 and precio > 0:
            acum[nk].append(_precio_unitario_desde_items_prov(precio, fac))
    return {
        nk: round(sum(vals) / len(vals), 6)
        for nk, vals in acum.items()
        if vals
    }


def recalcular(
    dry_run: bool = True,
    *,
    cod_mp_filtro: str | None = None,
    cod_bodega_filtro: str | None = None,
    solo_costo: bool = False,
    nombre_mp_buscar: str | None = None,
):
    print("=" * 55)
    print(f"RECALCULAR STOCK BD_MP_SISTEMA - {'DRY RUN' if dry_run else 'PRODUCCION'}")
    if cod_mp_filtro:
        print(f"  Filtro cod_mp_sistema: {cod_mp_filtro!r}")
    if cod_bodega_filtro:
        print(f"  Filtro cod_bodega: {cod_bodega_filtro!r}")
    if solo_costo:
        print("  Modo: solo costo_unitario_ref (no se toca stock_actual)")
    print("=" * 55)

    print("\n[1] Leyendo mov_inventario completo desde Supabase...")
    movs = paginar_todo(
        "mov_inventario",
        "cod_mp_sistema,tipo_mov,cantidad_mov,costo_unitario,fecha,cod_mov,"
        "cod_bodega_origen,cod_bodega_destino",
    )
    print(f"    {len(movs)} movimientos totales")

    dias_ventana = _dias_ventana_costo()
    cutoff: datetime | None = None
    if dias_ventana > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=dias_ventana)
        print(
            f"    Costo ref: promedio ponderado ENTRADAs (ultimos {dias_ventana} dias)"
        )
    else:
        print("    Costo ref: promedio ponderado ENTRADAs (historial completo)")

    stock_calculado: dict[tuple[str, str], float] = defaultdict(float)
    costo_ponderado_suma: dict[tuple[str, str], float] = defaultdict(float)
    cantidad_ponderada: dict[tuple[str, str], float] = defaultdict(float)
    # Costo ref único por MP (todas las bodegas comparten el mismo USD/unidad_base)
    costo_ponderado_mp: dict[str, float] = defaultdict(float)
    cantidad_mp: dict[str, float] = defaultdict(float)
    entradas_en_ventana = 0

    sin_bodega = 0
    for m in movs:
        cod = (m.get("cod_mp_sistema") or "").strip()
        if not cod:
            continue
        tipo = (m.get("tipo_mov") or "").strip()
        cantidad = float(m.get("cantidad_mov") or 0)
        costo = float(m.get("costo_unitario") or 0)
        fecha = (m.get("fecha") or "").strip()
        bod = _bodega_mov(m, tipo)

        if tipo in TIPOS_SUMA_DESTINO:
            if not bod:
                sin_bodega += 1
                continue
            k = _clave_stock(cod, bod)
            stock_calculado[k] += cantidad
        elif tipo in TIPOS_RESTA_ORIGEN:
            if not bod:
                sin_bodega += 1
                continue
            k = _clave_stock(cod, bod)
            stock_calculado[k] -= cantidad

        if (
            tipo == "ENTRADA"
            and costo > 0
            and bod
            and cantidad > 0
            and _fecha_dentro_ventana(fecha, cutoff)
        ):
            k = _clave_stock(cod, bod)
            costo_ponderado_suma[k] += costo * cantidad
            cantidad_ponderada[k] += cantidad
            nk = _cod_mp_norm(cod)
            if nk:
                costo_ponderado_mp[nk] += costo * cantidad
                cantidad_mp[nk] += cantidad
            entradas_en_ventana += 1

    costo_ref_mp: dict[tuple[str, str], float] = {}
    for k, qty in cantidad_ponderada.items():
        if qty > 0:
            costo_ref_mp[k] = round(costo_ponderado_suma[k] / qty, 6)

    costo_ref_unico_mp: dict[str, float] = {}
    for nk, qty in cantidad_mp.items():
        if qty > 0:
            costo_ref_unico_mp[nk] = round(costo_ponderado_mp[nk] / qty, 6)

    if sin_bodega:
        print(f"    WARN: {sin_bodega} movimientos sin bodega asignada (omitidos)")

    print(f"    {len(stock_calculado)} pares (MP, bodega) con stock calculado")
    print(
        f"    {len(costo_ref_mp)} pares con costo ref ponderado "
        f"({entradas_en_ventana} ENTRADAs en ventana)"
    )

    print("\n[2] Leyendo BD_MP_SISTEMA en Sheets...")
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = next(
        (i for i, r in enumerate(values) if any(c.strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if header_row_idx is None:
        print("ERROR: no se encontro header cod_mp_sistema")
        return

    headers = [h.strip() for h in values[header_row_idx]]
    try:
        col_cod = headers.index("cod_mp_sistema")
        col_bod = headers.index("cod_bodega")
        col_stock = headers.index("stock_actual") + 1
    except ValueError as e:
        print(f"ERROR columna no encontrada: {e}")
        return

    try:
        col_costo = headers.index("costo_unitario_ref") + 1
    except ValueError:
        col_costo = None
        print("  WARN: costo_unitario_ref no encontrada — solo stock")

    if nombre_mp_buscar and not cod_mp_filtro:
        res = _resolver_cod_mp_por_nombre_mp(
            values, header_row_idx, headers, col_cod, nombre_mp_buscar
        )
        if res:
            cod_mp_filtro = res
            print(f"  Resuelto por nombre_mp: cod_mp_sistema={cod_mp_filtro!r}")
        else:
            print(f"  ERROR: no se encontro MP para {nombre_mp_buscar!r}")
            return

    filtro_bod = normalizar_cod_bodega(cod_bodega_filtro) if cod_bodega_filtro else ""
    filtro_mp_norm = _cod_mp_norm(cod_mp_filtro) if cod_mp_filtro else ""

    from costo_mp_canonico import (
        cargar_costo_desde_items_prov,
        cargar_factor_items_prov,
        resolver_costo_ref_escritura,
    )

    prov_canon = cargar_costo_desde_items_prov(sh)
    factores_prov = cargar_factor_items_prov(sh)
    costo_fallback_prov = _cargar_costo_promedio_items_prov(sh)
    print(
        f"    Catálogo prov (canónico min): {len(prov_canon)} MPs | "
        f"fallback promedio: {len(costo_fallback_prov)}"
    )

    print("\n[3] Preparando updates...")
    updates = []
    data_rows = values[header_row_idx + 1:]
    filas_tocadas = 0

    for i, row in enumerate(data_rows):
        if not any(c.strip() for c in row):
            continue
        cod = row[col_cod].strip() if col_cod < len(row) else ""
        bod = row[col_bod].strip() if col_bod < len(row) else ""
        if not cod:
            continue

        if filtro_mp_norm and _cod_mp_norm(cod) != filtro_mp_norm:
            continue
        if filtro_bod and normalizar_cod_bodega(bod) != filtro_bod:
            continue

        filas_tocadas += 1
        row_1based = header_row_idx + i + 2
        k = _clave_stock(cod, bod)

        if not solo_costo and k in stock_calculado:
            nuevo_stock = round(stock_calculado[k], 4)
            stock_anterior_str = row[col_stock - 1].strip() if col_stock - 1 < len(row) else "0"
            try:
                from numeros_sheets import parse_numero_sheets

                stock_anterior = (
                    parse_numero_sheets(stock_anterior_str) if stock_anterior_str else 0.0
                )
            except ValueError:
                stock_anterior = 0.0

            updates.append({
                "range": rowcol_to_a1(row_1based, col_stock),
                "values": [[nuevo_stock]],
            })
            if abs(nuevo_stock - stock_anterior) > 0.001:
                print(f"    {cod} @ {bod}: stock {stock_anterior} -> {nuevo_stock}")

        if col_costo:
            nk = _cod_mp_norm(cod)
            costo_mov = costo_ref_unico_mp.get(nk) if nk else None
            if costo_mov is None:
                costo_mov = costo_ref_mp.get(k)
            costo_esc = resolver_costo_ref_escritura(
                costo_mov,
                prov_canon.get(nk or "", 0.0),
                factores_prov.get(nk or ""),
            )
            if costo_esc is None:
                costo_esc = costo_fallback_prov.get(nk or k[0])
            if costo_esc is not None and costo_esc > 0:
                updates.append({
                    "range": rowcol_to_a1(row_1based, col_costo),
                    "values": [[costo_esc]],
                })

    if cod_mp_filtro and filas_tocadas == 0:
        print(f"\n  WARN: ninguna fila para cod_mp={cod_mp_filtro!r}")

    print(f"\n    Filas evaluadas: {filas_tocadas} | celdas a actualizar: {len(updates)}")

    if dry_run:
        print("\n    [DRY RUN] No se escribio nada. Corre con --produccion para aplicar.")
        return

    if not updates:
        print("\n    Nada que escribir.")
        return

    print("\n[4] Escribiendo en Sheets...")
    batch_size = 50
    for i in range(0, len(updates), batch_size):
        lote = updates[i : i + batch_size]
        ws.batch_update(lote, value_input_option=ValueInputOption.user_entered)
        print(f"    Lote {i // batch_size + 1}: {len(lote)} celdas")
        if i + batch_size < len(updates):
            time.sleep(1)

    print(f"\nCompletado. {len(updates)} celdas actualizadas.")


def recalcular_produccion(
    *,
    cod_mp_filtro: str | None = None,
    cod_bodega_filtro: str | None = None,
) -> None:
    """Llamada desde tools (traslado WA, etc.)."""
    recalcular(
        dry_run=False,
        cod_mp_filtro=cod_mp_filtro,
        cod_bodega_filtro=cod_bodega_filtro,
    )


if __name__ == "__main__":
    import sys

    DRY_RUN = "--produccion" not in sys.argv
    solo_costo = "--solo-costo" in sys.argv

    cod_mp_filtro: str | None = None
    if "--cod-mp" in sys.argv:
        i = sys.argv.index("--cod-mp")
        if i + 1 < len(sys.argv):
            cod_mp_filtro = (sys.argv[i + 1] or "").strip()

    cod_bodega_filtro: str | None = None
    if "--cod-bodega" in sys.argv:
        i = sys.argv.index("--cod-bodega")
        if i + 1 < len(sys.argv):
            cod_bodega_filtro = (sys.argv[i + 1] or "").strip()

    nombre_mp_arg: str | None = None
    if "--nombre-mp" in sys.argv:
        i = sys.argv.index("--nombre-mp")
        if i + 1 < len(sys.argv):
            nombre_mp_arg = (sys.argv[i + 1] or "").strip()

    recalcular(
        dry_run=DRY_RUN,
        cod_mp_filtro=cod_mp_filtro,
        cod_bodega_filtro=cod_bodega_filtro,
        solo_costo=solo_costo,
        nombre_mp_buscar=nombre_mp_arg,
    )

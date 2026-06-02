"""Export mov_inventario de uno o varios MPs a CSV, con saldo y stock actual (post-pipeline)."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from supabase import create_client

from bodegas_config import normalizar_cod_bodega
from recalcular_stock_sheets import TIPOS_RESTA_ORIGEN, TIPOS_SUMA_DESTINO, _bodega_mov, _clave_stock, _cod_mp_norm

load_dotenv(override=True)

COLS = (
    "cod_mov,fecha,tipo_mov,cod_mp_sistema,nombre_mp,cod_bodega_origen,cod_bodega_destino,"
    "cantidad_mov,unidad_base,costo_unitario,costo_total,origen_documento,num_documento,"
    "registrado_por,observaciones"
)
EXTRA_COLS = ("stock_inicio_periodo", "saldo_despues_mov", "stock_actual_sheets", "fecha_ultimo_pipeline")
EXPORT_COLS = COLS + "," + ",".join(EXTRA_COLS)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CHECKPOINT = Path("logs/pipeline_checkpoint.json")


def _norm(c: str) -> str:
    return _cod_mp_norm(c)


def _parse_float(v) -> float:
    try:
        return float(str(v or "0").replace(",", "."))
    except ValueError:
        return 0.0


def _pipeline_meta() -> tuple[str, str]:
    if not CHECKPOINT.is_file():
        return "", ""
    try:
        data = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        return (data.get("updated_at") or "").strip(), (data.get("fecha_objetivo") or "").strip()
    except (json.JSONDecodeError, OSError):
        return "", ""


def _paginar_movs(sb, mp: str, hasta: str, select: str = COLS) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("mov_inventario")
            .select(select)
            .eq("cod_mp_sistema", mp)
            .lte("fecha", f"{hasta}T23:59:59")
            .order("fecha")
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


def _aplicar_mov(saldos: dict[tuple[str, str], float], m: dict) -> tuple[str, str] | None:
    cod = _cod_mp_norm(m.get("cod_mp_sistema") or "")
    tipo = (m.get("tipo_mov") or "").strip()
    cantidad = float(m.get("cantidad_mov") or 0)
    bod = _bodega_mov(m, tipo)
    if not cod or not bod:
        return None
    k = _clave_stock(cod, bod)
    if tipo in TIPOS_SUMA_DESTINO:
        saldos[k] += cantidad
    elif tipo in TIPOS_RESTA_ORIGEN:
        saldos[k] -= cantidad
    return k


def _cargar_stock_sheets(mps: set[str]) -> dict[tuple[str, str], float]:
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
        return {}
    headers = [h.strip() for h in values[header_row_idx]]
    idx_cod = headers.index("cod_mp_sistema")
    idx_bod = headers.index("cod_bodega")
    idx_stock = headers.index("stock_actual")
    out: dict[tuple[str, str], float] = {}
    for row in values[header_row_idx + 1 :]:
        cod = _cod_mp_norm(row[idx_cod] if idx_cod < len(row) else "")
        if cod not in mps:
            continue
        bod = normalizar_cod_bodega(row[idx_bod] if idx_bod < len(row) else "")
        out[(cod, bod)] = _parse_float(row[idx_stock] if idx_stock < len(row) else 0)
    return out


def export_mps(
    mps: list[str],
    desde: str,
    hasta: str,
    *,
    con_stock: bool = False,
    output: Path | None = None,
) -> Path:
    mps_norm = [_norm(m) for m in mps]
    mp_set = set(mps_norm)
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    pipeline_at, pipeline_fecha = _pipeline_meta()
    stock_sheets = _cargar_stock_sheets(mp_set) if con_stock else {}

    export_rows: list[dict] = []
    for mp in mps_norm:
        all_movs = _paginar_movs(sb, mp, hasta)
        saldos: dict[tuple[str, str], float] = defaultdict(float)
        inicio_periodo: dict[tuple[str, str], float] = {}
        saldo_por_mov: dict[str, float] = {}

        for m in all_movs:
            fecha = (m.get("fecha") or "")[:10]
            k = _aplicar_mov(saldos, m)
            if fecha < desde:
                if k:
                    inicio_periodo[k] = round(saldos[k], 4)
                continue
            if k:
                saldo_por_mov[m.get("cod_mov") or ""] = round(saldos[k], 4)
            row = dict(m)
            if con_stock:
                row["stock_inicio_periodo"] = inicio_periodo.get(k, 0.0) if k else ""
                row["saldo_despues_mov"] = saldo_por_mov.get(m.get("cod_mov") or "", "")
                row["stock_actual_sheets"] = stock_sheets.get(k, "") if k else ""
                row["fecha_ultimo_pipeline"] = pipeline_at
            export_rows.append(row)

    export_rows.sort(key=lambda r: (r.get("fecha") or "", r.get("cod_mp_sistema") or ""))

    out_dir = Path("logs")
    out_dir.mkdir(exist_ok=True)
    suffix = f"{desde.replace('-', '')}_{hasta.replace('-', '')}"
    if output:
        out_path = Path(output)
    elif len(mps_norm) == 1:
        tag = f"mp{mps_norm[0]}_mov_{suffix}"
        if con_stock:
            tag += "_con_stock"
        out_path = out_dir / f"{tag}.csv"
    else:
        tag = f"mps_{'_'.join(mps_norm)}_mov_{suffix}"
        if con_stock:
            tag += "_con_stock"
        out_path = out_dir / f"{tag}.csv"

    fields = EXPORT_COLS.split(",") if con_stock else COLS.split(",")
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in export_rows:
            w.writerow(r)

    print(f"MPs {', '.join(mps_norm)} | {desde} al {hasta} | filas: {len(export_rows)}")
    if con_stock:
        print(f"Ultimo pipeline: {pipeline_at or '?'} (fecha_objetivo {pipeline_fecha or '?'})")
    print(f"CSV: {out_path.resolve()}")
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description="Export mov_inventario por MP(s) a CSV")
    p.add_argument("cod_mp", nargs="?", help="cod_mp_sistema, ej. 268")
    p.add_argument("--mps", default=None, help="Varios MPs separados por coma, ej. 260,261,268")
    p.add_argument("--desde", required=True, help="YYYY-MM-DD")
    p.add_argument("--hasta", default=None, help="YYYY-MM-DD (default hoy)")
    p.add_argument("-o", "--output", default=None, help="Ruta CSV de salida")
    p.add_argument(
        "--con-stock",
        action="store_true",
        help="Incluye stock_inicio_periodo, saldo_despues_mov y stock_actual_sheets (post-pipeline)",
    )
    args = p.parse_args()

    if args.mps:
        mps = [x.strip() for x in args.mps.split(",") if x.strip()]
    elif args.cod_mp:
        mps = [args.cod_mp.strip()]
    else:
        p.error("Indica cod_mp o --mps")

    desde = args.desde.strip()[:10]
    hasta = (args.hasta or datetime.now().strftime("%Y-%m-%d")).strip()[:10]
    export_mps(mps, desde, hasta, con_stock=args.con_stock, output=Path(args.output) if args.output else None)


if __name__ == "__main__":
    main()

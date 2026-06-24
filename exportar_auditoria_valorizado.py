"""
Exporta BD_MP_SISTEMA a CSV para auditar el mismo cálculo que usa el agente
WhatsApp (inventario_valorizado): stock_actual × costo_unitario_ref.

Uso (desde tatami-agente, con .env y credenciales OK):
  python exportar_auditoria_valorizado.py
  python exportar_auditoria_valorizado.py -o C:\\temp\\auditoria.csv
  python exportar_auditoria_valorizado.py --solo-con-valor-en-agente
  python exportar_auditoria_valorizado.py --codigos 511,148,301,260,261,269 -o chequeo_mp.csv
  python exportar_auditoria_valorizado.py --codigos 261,269 --incluir-negativos-en-valor

El CSV incluye columnas derivadas (valor como el agente con stock>=0) para comparar con la hoja.

Separadores numéricos (Google Sheets suele mezclar formatos al exportar por API):
- stock_texto_hoja / costo_texto_hoja: texto tal cual viene de la celda.
- Los importes numéricos usan parse_sheet_number (sheet_numbers.py): coma o punto decimal,
  y si aparecen ambos, el separador más a la derecha es el decimal (ej. 1.234,56 → 1234,56).
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime

from dotenv import load_dotenv
import gspread
from sheet_numbers import parse_sheet_number
from google_credentials import google_credentials, has_google_credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _float(v) -> float:
    return parse_sheet_number(v, 0.0)


def cargar_filas_bd_mp_sistema() -> tuple[list[str], list[dict]]:
    load_dotenv(override=True)
    sid = os.getenv("SPREADSHEET_ID")
    if not has_google_credentials() or not sid:
        raise SystemExit("Faltan credenciales Google o SPREADSHEET_ID en .env")

    creds = google_credentials(SCOPES)
    sh = gspread.authorize(creds).open_by_key(sid)
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_mp_sistema" for c in row):
            header_row_idx = i
            break
    if header_row_idx is None:
        raise SystemExit("No se encontró fila header con cod_mp_sistema en BD_MP_SISTEMA")

    headers = [(c or "").strip() for c in values[header_row_idx]]
    data_rows = values[header_row_idx + 1 :]

    out: list[dict] = []
    for row in data_rows:
        if not any((c or "").strip() for c in row):
            continue
        r = {
            headers[j]: (row[j] if j < len(row) else "").strip()
            for j in range(len(headers))
        }
        if not r.get("cod_mp_sistema", "").strip():
            continue
        out.append(r)
    return headers, out


def _parse_codigos_filtro(raw: str) -> set[str] | None:
    if not (raw or "").strip():
        return None
    out: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if p:
            out.add(p)
    return out if out else None


def main() -> None:
    p = argparse.ArgumentParser(description="Exportar auditoría valorizado inventario (Sheets)")
    p.add_argument(
        "-o",
        "--output",
        default="",
        help="Ruta CSV (default: auditoria_valorizado_inventario_<timestamp>.csv en cwd)",
    )
    p.add_argument(
        "--solo-con-valor-en-agente",
        action="store_true",
        help="Solo filas que el agente sumaría en inventario_valorizado global (costo>0, stock≠0 por defecto)",
    )
    p.add_argument(
        "--incluir-negativos-en-valor",
        action="store_true",
        help="Usar stock tal cual para valor; por defecto max(stock,0) como la tool sin incluir_negativos",
    )
    p.add_argument(
        "--codigos",
        default="",
        help="Solo estos cod_mp_sistema (coma o punto y coma), ej. 511,148,301. No recalcula stock: solo lee la hoja.",
    )
    args = p.parse_args()

    headers_sheet, filas = cargar_filas_bd_mp_sistema()
    codigos_filtro = _parse_codigos_filtro(args.codigos)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = (args.output or "").strip() or f"auditoria_valorizado_inventario_{ts}.csv"

    # Columnas de interés si existen en la hoja (extras no rompen)
    extra_cols = [
        "par_level",
        "unidad_compra",
        "factor_conversion",
    ]
    cols_presentes = [c for c in extra_cols if c in headers_sheet]

    fieldnames = [
        "cod_mp_sistema",
        "nombre_mp",
        "cod_bodega",
        "unidad_base",
        "stock_texto_hoja",
        "costo_texto_hoja",
        "stock_actual_raw",
        "stock_para_valor_agente",
        "costo_unitario_ref",
        "valor_usd_agente_default",
        "sin_costo_ref",
        "suma_en_total_agente_global",
        "motivo_excluida_total",
    ] + cols_presentes

    written = 0
    codigos_exportados: set[str] = set()
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in filas:
            cod = r.get("cod_mp_sistema", "").strip()
            if codigos_filtro is not None and cod not in codigos_filtro:
                continue
            nom = r.get("nombre_mp", "").strip()
            bod = r.get("cod_bodega", "").strip()
            ubase = r.get("unidad_base", "").strip()
            stock_raw_txt = (r.get("stock_actual") or "").strip()
            costo_raw_txt = (r.get("costo_unitario_ref") or "").strip()
            stock = _float(stock_raw_txt)
            costo = _float(costo_raw_txt)

            stock_val = stock if args.incluir_negativos_en_valor else max(stock, 0.0)
            sin_costo = costo <= 0
            valor = stock_val * costo if not sin_costo else 0.0

            motivos = []
            if sin_costo:
                motivos.append("sin costo_unitario_ref o cero")
            if abs(stock) < 1e-9:
                motivos.append("stock_actual cero (tool global no lista esta fila)")
            suma_si = not sin_costo and abs(stock) >= 1e-9
            if args.solo_con_valor_en_agente and not suma_si:
                continue

            row_out = {
                "cod_mp_sistema": cod,
                "nombre_mp": nom,
                "cod_bodega": bod,
                "unidad_base": ubase,
                "stock_texto_hoja": stock_raw_txt,
                "costo_texto_hoja": costo_raw_txt,
                "stock_actual_raw": round(stock, 6),
                "stock_para_valor_agente": round(stock_val, 6),
                "costo_unitario_ref": round(costo, 8) if costo else 0.0,
                "valor_usd_agente_default": round(valor, 4),
                "sin_costo_ref": "SI" if sin_costo else "NO",
                "suma_en_total_agente_global": "SI" if suma_si else "NO",
                "motivo_excluida_total": "; ".join(motivos) if motivos else "",
            }
            for c in cols_presentes:
                row_out[c] = r.get(c, "").strip()
            w.writerow(row_out)
            written += 1
            codigos_exportados.add(cod)

    def _stock_v(r):
        s = _float(r.get("stock_actual"))
        return s if args.incluir_negativos_en_valor else max(s, 0.0)

    def _iter_filas_reporte():
        for r in filas:
            c = r.get("cod_mp_sistema", "").strip()
            if codigos_filtro is not None and c not in codigos_filtro:
                continue
            yield r

    total_valor = sum(
        _stock_v(r) * _float(r.get("costo_unitario_ref"))
        for r in _iter_filas_reporte()
        if _float(r.get("costo_unitario_ref")) > 0 and abs(_float(r.get("stock_actual"))) >= 1e-9
    )

    total_valor_con_neg = sum(
        _float(r.get("stock_actual")) * _float(r.get("costo_unitario_ref"))
        for r in _iter_filas_reporte()
        if _float(r.get("costo_unitario_ref")) > 0
    )

    print(f"Filas exportadas: {written}")
    print(f"Archivo: {os.path.abspath(out_path)}")
    if codigos_filtro:
        print(f"Filtro codigos: {sorted(codigos_filtro, key=lambda x: (len(x), x))}")
    print(
        f"Suma valor_usd (regla agente, stock cap>=0): {round(total_valor, 4)} "
        "| mismo subconjunto con stock real (incl. negativos): "
        f"{round(total_valor_con_neg, 4)}"
    )
    if codigos_filtro:
        faltan = codigos_filtro - codigos_exportados
        if faltan:
            print(f"WARN: cod_mp no encontrados en BD_MP_SISTEMA: {sorted(faltan)}")

    if codigos_filtro and not args.incluir_negativos_en_valor:
        negs = [
            r
            for r in _iter_filas_reporte()
            if _float(r.get("stock_actual")) < -1e-9 and _float(r.get("costo_unitario_ref")) > 0
        ]
        if negs:
            print(
                "INFO: hay stock negativo en el filtro; el agente (sin incluir_negativos) "
                "no suma ese valor en el total global. Usa --incluir-negativos-en-valor para valor teórico."
            )
            for r in negs:
                c = r.get("cod_mp_sistema", "").strip()
                s = _float(r.get("stock_actual"))
                co = _float(r.get("costo_unitario_ref"))
                print(f"    {c}: stock={s:g} × {co:g} = {round(s * co, 4)} USD (con negativo)")


if __name__ == "__main__":
    main()

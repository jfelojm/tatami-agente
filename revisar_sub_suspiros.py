"""Revisa costos SUB-048 suspiros y SUB-057 merengue italiano."""

from __future__ import annotations

import os

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from calcular_costo_subrecetas import (
    calcular_costos,
    cargar_costos_mp,
    resumen_subreceta_costo,
)
from calcular_costo_recetas import costo_linea_receta, cargar_unitarios_subreceta
from recetas_detalle import cargar_bd_recetas_detalle, es_linea_subreceta
from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas,
    cargar_bd_subrecetas_detalle,
)

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _match_suspiro(cod: str, nombre: str) -> bool:
    ck = (cod or "").replace("SUB-", "").strip()
    nom = (nombre or "").lower()
    return ck in ("048", "057") or "suspiro" in nom or "merengue" in nom


def main() -> None:
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])

    cab = cargar_bd_subrecetas(sh)
    det = cargar_bd_subrecetas_detalle(sh)
    por = agrupar_detalle_por_padre(det)
    costos_mp = cargar_costos_mp(sh)
    resultados, avisos = calcular_costos(cab, por, costos_mp)

    print("=== CABECERAS (suspiro / merengue) ===")
    for cod in sorted(cab.keys()):
        info = cab[cod]
        nom = info.get("nombre_subreceta") or ""
        if not _match_suspiro(cod, nom):
            continue
        r = resultados.get(cod, {})
        print(
            f"{cod:8} | {nom[:45]:45} | activa={info.get('activa', '')} | "
            f"rend={info.get('rendimiento_estandar', '')} {info.get('unidad', '')}"
        )
        print(
            f"         hoja: lote={info.get('costo_lote_estandar', '')} "
            f"unit={info.get('costo_unitario_estandar', '')} "
            f"calc_at={info.get('costo_calc_at', '')}"
        )
        print(
            f"         calc: lote={r.get('costo_lote', '?')} "
            f"unit={r.get('costo_unitario', '?')}"
        )

    print("\n=== DETALLE + DESGLOSE ===")
    for cod in sorted(por.keys()):
        info = cab.get(cod, {})
        nom = info.get("nombre_subreceta") or ""
        if not _match_suspiro(cod, nom):
            continue
        print(f"\n--- {cod} ({nom}) ---")
        for ln in por[cod]:
            print(
                f"  {ln.get('tipo_linea', '?')} "
                f"mp={ln.get('cod_mp_sistema', '')} "
                f"sub={ln.get('cod_subreceta_hijo', '')} "
                f"cant={ln.get('cantidad', '')} {ln.get('unidad_base', '')} "
                f"bod={ln.get('cod_bodega', '')} merma={ln.get('merma_pct', '')}"
            )
        s = resumen_subreceta_costo(cod, info, por[cod], costos_mp, resultados)
        for d in s.get("detalle_lineas", []):
            print(
                f"    -> {d['tipo']} {d['cod']} {d['nombre'][:35]} "
                f"cu={d['costo_unitario']} line={d['costo_linea']} {d.get('nota', '')}"
            )
        print(
            f"    TOTAL lote={s['costo_lote_estandar_usd']} "
            f"unit={s['costo_unitario_estandar_usd']} "
            f"notas={s.get('notas', '')}"
        )

    print("\n=== PLATOS QUE USAN SUB-048 ===")
    unitarios = cargar_unitarios_subreceta(sh)
    for ln in cargar_bd_recetas_detalle(sh):
        if not es_linea_subreceta(ln):
            continue
        cs = (ln.get("cod_subreceta") or "").strip()
        if cs not in ("048", "SUB-048", "057", "SUB-057"):
            if "048" not in cs and "057" not in cs:
                continue
        d = costo_linea_receta(ln, costos_mp, unitarios)
        print(
            f"  receta {ln.get('cod_receta')} {ln.get('nombre_receta', '')[:30]} | "
            f"sub={cs} cant={ln.get('cantidad')} | "
            f"costo_linea={d['costo_linea'] if d else '?'} {d.get('nota', '') if d else ''}"
        )

    print("\n=== AVISOS ===")
    rel = [a for a in avisos if _match_suspiro("", a)]
    if rel:
        for a in rel:
            print(" ", a)
    else:
        print("  (ninguno para suspiros/merengue)")


if __name__ == "__main__":
    main()

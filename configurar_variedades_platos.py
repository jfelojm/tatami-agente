"""
Configura variedades Smart Menu para Pad Thai, Tamago Rice y Bibimbap.

Regla: CLASICO = receta actual (variedad vacía). Extras = base + MPs adicionales:
  CAMARON → +80 g camarón (067)
  LOMO    → +100 g lomo Piggis (552)
  POLLO   → +100 g pechuga (051)
  HONGO   → +35 g champiñones (038) + 35 g portobelos (037)

Escribe BD_RECETAS_DETALLE y BD_PRODUCTOS en el maestro.

Uso:
  python configurar_variedades_platos.py --dry-run
  python configurar_variedades_platos.py --produccion
"""

from __future__ import annotations

import argparse
import copy
import sys

from dotenv import load_dotenv
from gspread.utils import ValueInputOption

from recetas_detalle import (
    HEADERS_BD_RECETAS_DETALLE,
    cargar_bd_recetas_detalle,
    es_linea_mp,
    norm_cod_receta,
)
from sheet_numbers import parse_sheet_number
from staging_common import open_master

load_dotenv(override=True)

VARIEDADES_EXTRA = ("CAMARON", "LOMO", "POLLO", "HONGO")

EXTRAS_MP: dict[str, list[tuple[str, str, str, str, str]]] = {
    "CAMARON": [("067", "CAMARON", "80", "gr", "BOD-001")],
    "LOMO": [("552", "LOMO FINO DE RES PIGGIS", "100", "gr", "BOD-001")],
    "POLLO": [("051", "PECHUGA DE POLLO", "100", "gr", "BOD-001")],
    "HONGO": [
        ("038", "CHAMPIÑONES", "35", "gr", "BOD-001"),
        ("037", "PORTOBELOS", "35", "gr", "BOD-001"),
    ],
}

PLATOS = (
    {
        "cod_receta": "008",
        "cod_smart_menu": "10",
        "nombre_receta": "PAD THAI",
        "nombre_producto": "PAD THAI",
    },
    {
        "cod_receta": "011",
        "cod_smart_menu": "14",
        "nombre_receta": "TAMAGO RICE",
        "nombre_producto": "TAMAGO RICE",
    },
    {
        "cod_receta": "012",
        "cod_smart_menu": "15",
        "nombre_receta": "BIBIMBAP COREANO",
        "nombre_producto": "BIBIMBAP COREANO",
    },
)


def _norm_cod(cod: str) -> str:
    s = (cod or "").strip()
    if s.isdigit():
        return str(int(s))
    return s


def _clave_linea(cod_receta: str, variedad: str, ln: dict) -> str:
    v = (variedad or "").strip().upper()
    cod = norm_cod_receta(cod_receta)
    sub = (ln.get("cod_subreceta") or "").strip()
    if sub:
        return f"{cod}|{v}|SUB:{sub}"
    mp = (ln.get("cod_mp_sistema") or "").strip()
    return f"{cod}|{v}|MP:{mp}"


def _lineas_base(lineas: list[dict], cod_receta: str) -> list[dict]:
    target = _norm_cod(cod_receta)
    return [
        ln
        for ln in lineas
        if _norm_cod(ln.get("cod_receta") or "") == target
        and not (ln.get("variedad_smart_menu") or "").strip()
    ]


def _indices_existentes(lineas: list[dict]) -> set[str]:
    out: set[str] = set()
    for ln in lineas:
        out.add(
            _clave_linea(
                ln.get("cod_receta") or "",
                ln.get("variedad_smart_menu") or "",
                ln,
            )
        )
    return out


def _fila_extra_mp(
    nombre_receta: str,
    cod_receta: str,
    variedad: str,
    cod_mp: str,
    nombre_mp: str,
    cantidad: str,
    unidad: str,
    bodega: str,
    plantilla_mp: dict | None,
) -> dict:
    tpl = plantilla_mp or {}
    return {
        "nombre_receta": nombre_receta,
        "cod_receta": cod_receta,
        "variedad_smart_menu": variedad,
        "nombre_subreceta": "",
        "cod_subreceta": "",
        "nombre_mp": nombre_mp,
        "cod_mp_sistema": cod_mp,
        "cantidad": cantidad,
        "unidad_base": unidad,
        "cod_bodega": bodega,
        "merma_pct": tpl.get("merma_pct") or "0",
        "es_opcional": tpl.get("es_opcional") or "NO",
        "pct_aplicacion": tpl.get("pct_aplicacion") or "1",
        "costo_unitario": "",
        "costo_linea": "",
        "nota_costo": "",
    }


def _plantilla_mp(lineas: list[dict], cod_mp: str) -> dict | None:
    for ln in lineas:
        if es_linea_mp(ln) and (ln.get("cod_mp_sistema") or "").strip() == cod_mp:
            return ln
    return None


def _fila_a_lista(row: dict) -> list[str]:
    return [row.get(h, "") for h in HEADERS_BD_RECETAS_DETALLE]


def _variety_configured(lineas: list[dict], cod_receta: str, variedad: str) -> bool:
    target = _norm_cod(cod_receta)
    return any(
        _norm_cod(ln.get("cod_receta") or "") == target
        and (ln.get("variedad_smart_menu") or "").strip().upper() == variedad
        for ln in lineas
    )


def _generar_filas_receta(
    plato: dict, base: list[dict], todas_lineas: list[dict], existentes: set[str]
) -> tuple[list[dict], list[str]]:
    nuevas: list[dict] = []
    log: list[str] = []
    cod_rec = plato["cod_receta"]
    nom = plato["nombre_receta"]

    if not base:
        log.append(f"  ERROR {nom}: sin líneas base (variedad vacía)")
        return nuevas, log

    for variedad in VARIEDADES_EXTRA:
        if _variety_configured(todas_lineas, cod_rec, variedad):
            log.append(f"  SKIP {nom} / {variedad}: ya configurado")
            continue

        bloque: list[dict] = []
        for ln in base:
            clon = copy.deepcopy(ln)
            clon["variedad_smart_menu"] = variedad
            clon["nombre_receta"] = nom
            clon["costo_unitario"] = ""
            clon["costo_linea"] = ""
            clon["nota_costo"] = ""
            bloque.append(clon)

        for cod_mp, nombre_mp, cant, uni, bod in EXTRAS_MP[variedad]:
            extra_cant = parse_sheet_number(cant, 0.0)
            sumado = False
            for ln in bloque:
                if es_linea_mp(ln) and (ln.get("cod_mp_sistema") or "").strip() == cod_mp:
                    prev = parse_sheet_number(ln.get("cantidad"), 0.0)
                    ln["cantidad"] = str(prev + extra_cant)
                    sumado = True
                    break
            if not sumado:
                bloque.append(
                    _fila_extra_mp(
                        nom,
                        cod_rec,
                        variedad,
                        cod_mp,
                        nombre_mp,
                        cant,
                        uni,
                        bod,
                        _plantilla_mp(base, cod_mp),
                    )
                )

        n_var = 0
        for ln in bloque:
            clave = _clave_linea(cod_rec, variedad, ln)
            if clave in existentes:
                continue
            nuevas.append(ln)
            existentes.add(clave)
            n_var += 1
        if n_var:
            log.append(f"  + {nom} / {variedad}: {n_var} líneas nuevas")

    return nuevas, log


def _productos_faltantes(
    values: list[list[str]], hi: int, headers: list[str], plato: dict
) -> list[list[str]]:
    cod_sm = plato["cod_smart_menu"]
    idx_sm = headers.index("cod_smart_menu")
    idx_var = headers.index("variedad_smart_menu")

    template_row: list[str] | None = None
    existentes_var: set[str] = set()
    for row in values[hi + 1 :]:
        if len(row) <= idx_sm:
            continue
        if (row[idx_sm] or "").strip() != cod_sm:
            continue
        var = (row[idx_var] if len(row) > idx_var else "").strip().upper()
        existentes_var.add(var)
        if not var and template_row is None:
            template_row = row

    if template_row is None:
        for row in values[hi + 1 :]:
            if len(row) > idx_sm and (row[idx_sm] or "").strip() == cod_sm:
                template_row = row
                break

    if template_row is None:
        return []

    nuevas: list[list[str]] = []
    ncols = len(headers)
    for var in VARIEDADES_EXTRA:
        if var in existentes_var:
            continue
        fila = list(template_row)
        while len(fila) < ncols:
            fila.append("")
        fila[idx_var] = var
        nuevas.append(fila[:ncols])
    return nuevas


def run(*, dry_run: bool) -> int:
    ma = open_master()
    lineas = cargar_bd_recetas_detalle(ma)
    existentes = _indices_existentes(lineas)

    todas_recetas: list[dict] = []
    print("BD_RECETAS_DETALLE:")
    for plato in PLATOS:
        base = _lineas_base(lineas, plato["cod_receta"])
        print(f"  {plato['nombre_receta']} (receta {plato['cod_receta']}): {len(base)} líneas base")
        nuevas, log = _generar_filas_receta(plato, base, lineas, existentes)
        for msg in log:
            print(msg)
        todas_recetas.extend(nuevas)

    ws_det = ma.worksheet("BD_RECETAS_DETALLE")
    if todas_recetas and not dry_run:
        ws_det.append_rows(
            [_fila_a_lista(r) for r in todas_recetas],
            value_input_option=ValueInputOption.user_entered,
        )
        print(f"\n  Insertadas {len(todas_recetas)} líneas en BD_RECETAS_DETALLE")
    elif todas_recetas:
        print(f"\n  [DRY] Insertaría {len(todas_recetas)} líneas en BD_RECETAS_DETALLE")

    ws_prod = ma.worksheet("BD_PRODUCTOS")
    pvals = ws_prod.get_all_values()
    phi = next(
        i for i, r in enumerate(pvals) if any(c.strip() == "cod_smart_menu" for c in r)
    )
    pheaders = [c.strip() for c in pvals[phi]]

    todas_prod: list[list[str]] = []
    print("\nBD_PRODUCTOS:")
    for plato in PLATOS:
        falt = _productos_faltantes(pvals, phi, pheaders, plato)
        for row in falt:
            var = row[pheaders.index("variedad_smart_menu")]
            print(f"  + {plato['nombre_producto']} / {var}")
        todas_prod.extend(falt)

    if todas_prod and not dry_run:
        ws_prod.append_rows(
            todas_prod,
            value_input_option=ValueInputOption.user_entered,
        )
        print(f"\n  Insertadas {len(todas_prod)} filas en BD_PRODUCTOS")
    elif todas_prod:
        print(f"\n  [DRY] Insertaría {len(todas_prod)} filas en BD_PRODUCTOS")

    if not dry_run and (todas_recetas or todas_prod):
        print("\n  Sugerencia: python calcular_costo_recetas.py --produccion")

    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)
    sys.exit(run(dry_run=args.dry_run or not args.produccion))


if __name__ == "__main__":
    main()

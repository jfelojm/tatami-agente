"""
Audita costo_lote_estandar y costo_unitario_estandar en BD_SUBRECETAS.

Compara valores en hoja vs recálculo desde MPs + hijos SUB.
Marca: unit != lote/rendimiento, diferencia vs cálculo, costo unitario absurdo.

Uso:
  python auditar_costos_subrecetas.py
  python auditar_costos_subrecetas.py --cod 010 004
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from calcular_costo_subrecetas import cargar_contexto_subrecetas, resumen_subreceta_costo
from numeros_sheets import parse_numero_sheets

load_dotenv(override=True)

ACTIVAS = frozenset({"SI", "S", "YES", "1", "TRUE"})


def _norm_cod(c: str) -> str:
    s = (c or "").strip()
    if s.isdigit():
        return str(int(s))
    return s


def auditar(*, codigos: list[str] | None = None) -> int:
    cab, por_padre, costos_mp, resultados = cargar_contexto_subrecetas()
    problemas: list[str] = []
    ok_count = 0

    print("=== Auditoría costos subrecetas (hoja vs recálculo MPs) ===\n")

    keys = sorted(cab.keys(), key=lambda c: (int(c) if c.isdigit() else 9999, c))
    if codigos:
        want = {_norm_cod(c) for c in codigos}
        keys = [k for k in keys if _norm_cod(k) in want]

    for cod in keys:
        info = cab[cod]
        if (info.get("activa") or "SI").strip().upper() not in ACTIVAS:
            continue

        nom = (info.get("nombre_subreceta") or "").strip()
        rend = parse_numero_sheets(info.get("rendimiento_estandar"))
        hoja_lote = parse_numero_sheets(info.get("costo_lote_estandar"))
        hoja_unit = parse_numero_sheets(info.get("costo_unitario_estandar"))
        calc = resultados.get(cod) or resultados.get(_norm_cod(cod)) or {}
        cl_calc = float(calc.get("costo_lote") or 0)
        cu_calc = float(calc.get("costo_unitario") or 0)

        issues: list[str] = []
        if rend > 0 and hoja_lote > 0:
            esperado_unit = hoja_lote / rend
            if abs(hoja_unit - esperado_unit) > max(0.0001, esperado_unit * 0.02):
                issues.append(
                    f"unit_hoja ({hoja_unit:.4f}) != lote/rend ({esperado_unit:.4f})"
                )
        if cu_calc > 0 and abs(hoja_lote - cl_calc) > max(0.5, cl_calc * 0.05):
            issues.append(f"lote hoja {hoja_lote:.2f} vs calc {cl_calc:.2f}")
        if cu_calc > 0 and hoja_unit > cu_calc * 50:
            issues.append(f"unit_hoja inflado vs calc {cu_calc:.6f}")
        if cu_calc > 100:
            issues.append(f"unit_calc muy alto {cu_calc:.2f} (revisar MPs)")
        if cl_calc <= 0:
            issues.append("sin costo calculable (MPs sin costo ref)")

        if issues:
            problemas.append(cod)
            print(f"[REVISAR] {cod} {nom}")
            print(f"  Hoja:  lote={hoja_lote:.4f}  unit={hoja_unit:.6f}  rend={rend} gr/ml/uni")
            print(f"  Calc:  lote={cl_calc:.4f}  unit={cu_calc:.6f}")
            for iss in issues:
                print(f"    - {iss}")
            lineas = por_padre.get(cod) or por_padre.get(_norm_cod(cod)) or []
            if lineas:
                r = resumen_subreceta_costo(
                    cod, info, lineas, costos_mp, resultados
                )
                print("  Desglose lote estándar:")
                for ln in r.get("detalle_lineas", [])[:12]:
                    nota = ln.get("nota") or ""
                    print(
                        f"    {ln['tipo']:3} {ln['cod']:6} "
                        f"cant={ln['cantidad']:8.2f} "
                        f"cu_mp={ln['costo_unitario']:10.6f} "
                        f"linea=${ln['costo_linea']:8.2f} {nota}"
                    )
                if r.get("notas"):
                    print(f"  Notas: {r['notas'][:200]}")
            print()
        else:
            ok_count += 1

    print(f"OK coherentes: {ok_count}")
    print(f"A revisar: {len(problemas)}")
    if problemas:
        print("Códigos:", ", ".join(problemas))
    return 1 if problemas else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cod", nargs="*", help="Solo estos cod_subreceta")
    args = p.parse_args()
    raise SystemExit(auditar(codigos=args.cod or None))


if __name__ == "__main__":
    main()

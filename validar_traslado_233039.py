"""Valida duplicados y stock antes del deploy — TRA-20260625-233039."""
from __future__ import annotations

from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(override=True)

from bodegas_config import normalizar_cod_bodega
from inventario_stock_mp import norm_mp
from numeros_sheets import parse_numero_sheets
from recalcular_stock_sheets import _clave_stock, build_stock_calculado, paginar_todo
from whatsapp_webhook import leer_bd_mp_sistema

ORIG = "BOD-005"
DEST = "BOD-001"

ESPERADO = {
    "069": 60,
    "027": 1747,
    "151": 400,
    "104": 1200,
    "048": 3200,
    "087": 360,
    "019": 1800,
    "047": 5400,
    "552": 2000,
    "SUB-008": 3500,
    "127": 4540,
    "081": 4540,
    "097": 10000,
    "SUB-061": 1054,
}

# Documentos válidos que deben quedar (1 par SAL+ENT cada uno)
DOCS_VALIDOS = {
    "TRA-20260625232853",  # lechuga 151
    "TRA-20260625232854",  # vino 104
    "TRA-20260625233552-001",
    "TRA-20260625233553-002",
    "TRA-20260625233554-003",
    "TRA-20260625233554-004",
    "TRA-20260625233557-005",
    "TRA-20260625233558-006",
    "TRA-20260625233558-007",
    "TRA-20260625233558-008",
    "TRA-20260625233559-009",
}


def _norm_cod(c: str) -> str:
    c = (c or "").strip()
    if c.upper().startswith("SUB-"):
        return c.upper()
    return norm_mp(c)


def _parse_stock(v) -> float:
    try:
        return parse_numero_sheets(v, 0.0)
    except (TypeError, ValueError):
        return 0.0


def main():
    movs = paginar_todo(
        "mov_inventario",
        "cod_mov,cod_mp_sistema,tipo_mov,cantidad_mov,cod_bodega_origen,cod_bodega_destino,"
        "num_documento,registrado_por,fecha",
    )

    print("=" * 60)
    print("VALIDACION TRASLADO TRA-20260625-233039 (post-correccion)")
    print("=" * 60)

    # Traslados del lote (SHEETS, RETRY, CORRECCION) 25-jun 005->001
    lote: list[dict] = []
    for m in movs:
        por = m.get("registrado_por") or ""
        doc = m.get("num_documento") or ""
        if not any(
            por.startswith(p)
            for p in ("SHEETS:", "RETRY:", "CORRECCION:TRA-20260625-233039")
        ):
            continue
        if "20260625" not in doc and "20260625" not in por:
            continue
        t = m.get("tipo_mov")
        o = normalizar_cod_bodega(m.get("cod_bodega_origen") or "")
        d = normalizar_cod_bodega(m.get("cod_bodega_destino") or "")
        if t == "TRASLADO_SALIDA" and o == ORIG:
            lote.append(m)
        elif t == "TRASLADO_ENTRADA" and d == DEST:
            lote.append(m)

    print(f"\n[1] Movimientos lote activos: {len(lote)}")
    docs_vivos = sorted({m.get("num_documento") for m in lote})
    print(f"    Documentos: {len(docs_vivos)}")
    for doc in docs_vivos:
        ms = [m for m in lote if m.get("num_documento") == doc]
        flag = " OK" if doc in DOCS_VALIDOS or doc.startswith("TRA-202606252344") else " ?"
        print(
            f"      {doc}{flag}: "
            + ", ".join(
                f"MP{m['cod_mp_sistema']} {m['tipo_mov'][:3]} {m['cantidad_mov']}"
                for m in ms
            )
        )

    # Duplicados por MP+cantidad en SAL
    sal = defaultdict(list)
    for m in lote:
        if m["tipo_mov"] != "TRASLADO_SALIDA":
            continue
        sal[(_norm_cod(m["cod_mp_sistema"]), float(m["cantidad_mov"]))].append(
            m["num_documento"]
        )

    print("\n[2] Duplicados por MP+cantidad")
    dupes = 0
    for (cod, cant), docs in sorted(sal.items()):
        if len(docs) > 1:
            dupes += 1
            print(f"    DUPLICADO MP{cod} cant={cant}: {docs}")
    if not dupes:
        print("    (ninguno)")

    print("\n[3] Neto SAL 005->001 vs esperado (1x)")
    neto: dict[str, float] = defaultdict(float)
    for m in lote:
        if m["tipo_mov"] != "TRASLADO_SALIDA":
            continue
        neto[_norm_cod(m["cod_mp_sistema"])] += float(m["cantidad_mov"] or 0)

    neto_ok = True
    for cod, esp in sorted(ESPERADO.items(), key=lambda x: x[0]):
        n = neto.get(_norm_cod(cod), 0)
        ok = abs(n - esp) < 0.01
        if not ok:
            neto_ok = False
        mark = "OK" if ok else f"DIFF {n - esp:+.2f}"
        print(f"    {cod:8} esp={esp:>8} neto={n:>10.2f}  {mark}")

    extras = set(neto) - {_norm_cod(c) for c in ESPERADO}
    if extras:
        print("    MPs extra:", {k: neto[k] for k in sorted(extras)})

    print("\n[4] Stock hoja vs calculado (tol 0.05)")
    rows = leer_bd_mp_sistema(force_refresh=True)
    calc = build_stock_calculado(movs)
    stock_issues = 0
    for cod in sorted(ESPERADO.keys()):
        nc = _norm_cod(cod)
        for bod in (ORIG, DEST):
            hs = None
            for r in rows:
                rc = (r.get("cod_mp_sistema") or "").strip()
                if rc != cod and norm_mp(rc) != nc and rc.upper() != nc:
                    continue
                if normalizar_cod_bodega(r.get("cod_bodega")) != bod:
                    continue
                hs = _parse_stock(r.get("stock_actual"))
                break
            cv = calc.get(_clave_stock(cod if cod.startswith("SUB") else nc, bod), 0.0)
            if hs is None:
                print(f"    {cod:8} {bod}: sin fila | calc={cv:.2f}")
                continue
            d = abs(hs - cv)
            flag = "OK" if d <= 0.05 else f"DIFF {hs - cv:+.2f}"
            if d > 0.05:
                stock_issues += 1
            print(f"    {cod:8} {bod}: hoja={hs:>12.2f} calc={cv:>12.2f}  {flag}")

    print("\n" + "=" * 60)
    all_ok = dupes == 0 and neto_ok and stock_issues == 0
    print(
        f"Duplicados={dupes} | Neto={'OK' if neto_ok else 'FAIL'} | "
        f"Stock={'OK' if stock_issues == 0 else f'{stock_issues} diffs'}"
    )
    print(f"LISTO PARA DEPLOY: {'SI' if all_ok else 'NO'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

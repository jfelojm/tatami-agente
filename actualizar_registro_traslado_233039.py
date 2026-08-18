"""Actualiza REGISTRO_TRASLADOS TRA-20260625-233039 con estado real post-corrección."""
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(override=True)

from recalcular_stock_sheets import paginar_todo
from staging_common import open_staging

TRX = "TRA-20260625-233039"

# producto parcial -> cod_mp esperado (para mapear cod_mov)
PRODUCTO_COD = [
    ("HUEVOS", "069", 60),
    ("NABO", "027", 1747),
    ("MANTEQUILLA", "087", 360),
    ("CREMA DE LECHE", "019", 1800),
    ("LECHUGA", "151", 400),
    ("LOMO FINO DE RES ITALIANA", "047", 5400),
    ("LOMO FINO DE RES PIGGIS", "552", 2000),
    ("pollo en salmuera", "SUB-008", 3500),
    ("SALMON", "127", 4540),
    ("VINO SHAOXING", "104", 1200),
    ("SALSA DE OSTRAS", "081", 4540),
    ("FIDEO DE ARROZ", "097", 10000),
    ("torta de chocolate", "SUB-061", 1054),
    ("LOMO FINO CERDO", "048", 3200),
]

movs = paginar_todo(
    "mov_inventario",
    "cod_mov,cod_mp_sistema,tipo_mov,cantidad_mov,num_documento,registrado_por",
)

# SAL 005->001 del lote corregido
lote_sal = [
    m
    for m in movs
    if m.get("tipo_mov") == "TRASLADO_SALIDA"
    and any(
        (m.get("registrado_por") or "").startswith(p)
        for p in ("SHEETS:", "RETRY:", "CORRECCION:TRA-20260625-233039")
    )
    and "20260625" in (m.get("num_documento") or "")
]

# indexar por (cod_mp, cantidad)
por_clave = {}
for m in lote_sal:
    cod = (m.get("cod_mp_sistema") or "").strip()
    cant = float(m.get("cantidad_mov") or 0)
    doc = (m.get("num_documento") or "").strip()
    por_clave[(cod, cant)] = doc
    # también norm numérico
    if cod.isdigit() or (cod.lstrip("0").isdigit() and not cod.startswith("SUB")):
        por_clave[(cod.lstrip("0") or "0", cant)] = doc
        por_clave[(cod.zfill(3), cant)] = doc


def buscar_doc(cod_mp: str, cant: float) -> str:
    for k in [(cod_mp, cant), (cod_mp.lstrip("0"), cant), (cod_mp.zfill(3) if not cod_mp.startswith("SUB") else cod_mp, cant)]:
        if k in por_clave:
            return por_clave[k]
    return ""


def main():
    sh = open_staging()
    ws = sh.worksheet("REGISTRO_TRASLADOS")
    rows = ws.get_all_values()
    hi = next(i for i, r in enumerate(rows) if r and r[0].lower() == "trx")
    headers = rows[hi]
    col_estado = headers.index("estado")
    col_cod_mp = headers.index("cod_mp_sistema")
    col_mov = headers.index("cod_mov")

    updates = 0
    for i, r in enumerate(rows[hi + 1 :], start=hi + 2):
        if not r or (r[0] or "").strip() != TRX:
            continue
        producto = (r[5] if len(r) > 5 else "").upper()
        cant = float(r[7]) if len(r) > 7 and r[7] else 0

        cod_mp = ""
        for frag, cod, c in PRODUCTO_COD:
            if frag.upper() in producto and abs(cant - c) < 0.01:
                cod_mp = cod
                break
        if not cod_mp:
            cod_mp = (r[6] if len(r) > 6 else "").strip()

        doc = buscar_doc(cod_mp, cant)
        ws.update_cell(i, col_estado + 1, "OK")
        ws.update_cell(i, col_cod_mp + 1, cod_mp)
        if doc:
            ws.update_cell(i, col_mov + 1, doc)
        updates += 1
        print(f"  fila {i}: OK MP{cod_mp} cant={cant} mov={doc or '(sin doc)'}")

    print(f"\nActualizadas {updates} filas de {TRX}")


if __name__ == "__main__":
    main()

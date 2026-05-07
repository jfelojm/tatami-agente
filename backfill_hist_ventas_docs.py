"""
Backfill hist_ventas_docs desde hist_ventas ya cargado.

Requiere que exista la tabla hist_ventas_docs en Supabase.
Agrupa por (fecha, num_documento) y suma total de líneas (hist_ventas.total).

Nota: esto NO es el subtotal oficial sin IVA por documento (Smart Menu grid).
Eso se puede agregar después como columna extra y otro backfill.
"""

import os
from collections import defaultdict

from dotenv import load_dotenv
from supabase import create_client


load_dotenv(override=True)


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        try:
            return float(str(v).replace(",", "."))
        except Exception:
            return 0.0


def main():
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

    print("Leyendo hist_ventas (paginado)...")
    offset = 0
    page = 2000
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"total_items": 0.0, "lineas": 0})

    while True:
        res = (
            sb.table("hist_ventas")
            .select("fecha,num_documento,total")
            .range(offset, offset + page - 1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            break
        for r in rows:
            fecha = (r.get("fecha") or "").strip()
            num = (r.get("num_documento") or "").strip()
            if not fecha or not num:
                continue
            key = (fecha, num)
            agg[key]["total_items"] += _safe_float(r.get("total"))
            agg[key]["lineas"] += 1

        offset += page
        if len(rows) < page:
            break
        if offset % 20000 == 0:
            print(f"  ... {offset} filas leídas")

    print(f"Documentos agregados: {len(agg)}")

    # Upsert por lotes
    docs = []
    for (fecha, num), v in agg.items():
        docs.append(
            {
                "fecha": fecha,
                "num_documento": num,
                "total_items": round(v["total_items"], 4),
                "lineas": int(v["lineas"]),
            }
        )

    print("Insertando/actualizando hist_ventas_docs (upsert)...")
    batch = 500
    ok = 0
    for i in range(0, len(docs), batch):
        lote = docs[i : i + batch]
        sb.table("hist_ventas_docs").upsert(
            lote, on_conflict="fecha,num_documento"
        ).execute()
        ok += len(lote)
        print(f"  {ok}/{len(docs)}")

    print("Listo.")


if __name__ == "__main__":
    main()


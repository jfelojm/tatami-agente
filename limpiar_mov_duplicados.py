import os
from collections import defaultdict
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def paginar_todo(tabla, select, filters=None):
    rows = []
    offset = 0
    while True:
        q = supabase.table(tabla).select(select)
        if filters:
            for method, *args in filters:
                q = getattr(q, method)(*args)
        chunk = q.range(offset, offset + 999).execute().data
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def limpiar_duplicados(dry_run=True):
    print("=" * 55)
    print(f"LIMPIAR MOV_INVENTARIO DUPLICADOS - {'DRY RUN' if dry_run else 'PRODUCCION'}")
    print("=" * 55)

    print("\n[1] Leyendo mov_inventario tipo ENTRADA...")
    entradas = paginar_todo(
        "mov_inventario",
        "cod_mov,num_documento,cod_mp_sistema,tipo_mov,fecha",
        filters=[("eq", "tipo_mov", "ENTRADA")]
    )
    print(f"    {len(entradas)} filas ENTRADA encontradas")

    # Agrupar por clave natural: num_documento + cod_mp_sistema
    grupos = defaultdict(list)
    for row in entradas:
        clave = (
            (row.get("num_documento") or "").strip(),
            (row.get("cod_mp_sistema") or "").strip(),
        )
        grupos[clave].append(row["cod_mov"])

    # Identificar duplicados
    a_borrar = []
    for (num_doc, cod_mp), cod_movs in grupos.items():
        if len(cod_movs) <= 1:
            continue
        # Conservar el primero (menor cod_mov = más antiguo por formato MOV-YYYYMMDD-)
        cod_movs_sorted = sorted(cod_movs)
        conservar = cod_movs_sorted[0]
        duplicados = cod_movs_sorted[1:]
        a_borrar.extend(duplicados)
        print(f"    {num_doc} | {cod_mp}: {len(cod_movs)} filas -> conserva {conservar}, borra {len(duplicados)}")

    print(f"\n[2] Total a conservar: {len(entradas) - len(a_borrar)}")
    print(f"    Total a borrar:    {len(a_borrar)}")

    if not a_borrar:
        print("\n    Sin duplicados encontrados.")
        return

    if dry_run:
        print(f"\n    [DRY RUN] Se borrarían {len(a_borrar)} filas — corre sin --dry-run para aplicar")
        return

    print(f"\n[3] Borrando {len(a_borrar)} filas duplicadas...")
    borrados = 0
    errores = 0
    batch_size = 50
    for i in range(0, len(a_borrar), batch_size):
        lote = a_borrar[i: i + batch_size]
        try:
            supabase.table("mov_inventario").delete().in_("cod_mov", lote).execute()
            borrados += len(lote)
            print(f"    Lote {i // batch_size + 1}: {len(lote)} borrados (total={borrados})")
        except Exception as e:
            errores += len(lote)
            print(f"    ERROR en lote {i // batch_size + 1}: {e}")

    print(f"\n[4] Completado: {borrados} borrados | {errores} errores")

    # Verificar total final
    total = paginar_todo("mov_inventario", "cod_mov")
    print(f"    Total filas mov_inventario ahora: {len(total)}")


if __name__ == "__main__":
    import sys
    DRY_RUN = "--dry-run" not in sys.argv or "--produccion" not in sys.argv
    # Por seguridad, default es dry-run. Pasar --produccion para ejecutar real.
    DRY_RUN = "--produccion" not in sys.argv
    limpiar_duplicados(dry_run=DRY_RUN)

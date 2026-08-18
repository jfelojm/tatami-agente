"""
Sincroniza Masters Sheets (staging) desde el maestro operativo (SPREADSHEET_ID).

Flujo maestro -> staging para captura operativa:
  1. BD_MP_SISTEMA: filas SUB-* alineadas a BD_SUBRECETAS (maestro)
  2. CAT_TRASLADO + lista H en INGRESO_TRASLADO (traslados masivos)
  3. CAT_FM en INGRESO_FACTURA (facturas manuales Sumba/Loja/Inguil)

No promueve STAGING_* al maestro (eso es staging -> maestro, menú Tatami Admin).

Uso:
  python staging_sync_desde_maestro.py
  python staging_sync_desde_maestro.py --dry-run
  python staging_sync_desde_maestro.py --skip-sub-sync
  python staging_sync_desde_maestro.py --full-setup
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from setup_ingreso_factura_manual import actualizar_catalogo_factura
from setup_ingreso_traslado_masivo import actualizar_catalogo_traslado
from staging_common import sheets_api, staging_spreadsheet_id

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _sync_sub_pseudo_mp(*, dry_run: bool) -> dict:
    from sync_stock_subrecetas_maestro import sync

    return sync(dry_run=dry_run)


def sync_staging_desde_maestro(
    *,
    dry_run: bool = False,
    skip_sub_sync: bool = False,
    full_setup: bool = False,
) -> dict:
    sid = staging_spreadsheet_id()
    resumen: dict = {"staging_id": sid, "dry_run": dry_run}

    if not skip_sub_sync:
        log.info("Paso 1/3: pseudo-MP SUB-* en BD_MP_SISTEMA (maestro)")
        resumen["sub_sync"] = _sync_sub_pseudo_mp(dry_run=dry_run)
    else:
        log.info("Paso 1/3: omitido (--skip-sub-sync)")
        resumen["sub_sync"] = None

    if dry_run:
        log.info("DRY-RUN: no se escriben catálogos en Masters Sheets")
        resumen["traslados"] = {"items": "(dry-run)"}
        resumen["facturas"] = {"items": "(dry-run)"}
        return resumen

    sheets = sheets_api()

    if full_setup:
        log.info("Paso 2-3/3: setup completo INGRESO_TRASLADO + INGRESO_FACTURA")
        from setup_ingreso_factura_manual import main as setup_facturas
        from setup_ingreso_traslado_masivo import main as setup_traslados

        setup_traslados()
        setup_facturas()
        resumen["modo"] = "full-setup"
        return resumen

    log.info("Paso 2/3: CAT_TRASLADO + lista H (traslados)")
    resumen["traslados"] = actualizar_catalogo_traslado(sheets, sid)

    log.info("Paso 3/3: CAT_FM (facturas manuales)")
    resumen["facturas"] = actualizar_catalogo_factura(sheets, sid)
    resumen["modo"] = "solo-catalogos"
    return resumen


def main() -> int:
    p = argparse.ArgumentParser(
        description="Maestro operativo -> catálogos Masters Sheets (traslados y facturas)"
    )
    p.add_argument("--dry-run", action="store_true", help="Solo simula SUB sync; no escribe staging")
    p.add_argument(
        "--skip-sub-sync",
        action="store_true",
        help="No ejecutar sync_stock_subrecetas_maestro en el maestro",
    )
    p.add_argument(
        "--full-setup",
        action="store_true",
        help="Reconfigura hojas INGRESO_* completas (más lento; primera vez o cambio de layout)",
    )
    args = p.parse_args()

    res = sync_staging_desde_maestro(
        dry_run=args.dry_run,
        skip_sub_sync=args.skip_sub_sync,
        full_setup=args.full_setup,
    )

    sid = res["staging_id"]
    print("\n" + "=" * 72)
    print("  OK  Maestro -> Masters Sheets")
    print(f"  Staging: https://docs.google.com/spreadsheets/d/{sid}")
    if res.get("sub_sync"):
        ss = res["sub_sync"]
        print(
            f"  SUB en maestro: creadas={ss.get('creadas', 0)} "
            f"actualizadas={ss.get('actualizadas', 0)}"
        )
    tr = res.get("traslados") or {}
    if isinstance(tr.get("items"), int):
        print(f"  CAT_TRASLADO: {tr['items']} items | lista H: {tr.get('lista_h', 0)}")
        if tr.get("por_bodega"):
            print(f"    por bodega: {tr['por_bodega']}")
    fm = res.get("facturas") or {}
    if isinstance(fm.get("items"), int):
        print(f"  CAT_FM: {fm['items']} items")
    print()
    print("  En Masters Sheets:")
    print("    Traslados -> menu Tatami Traslados -> Filtrar lista por bodega origen")
    print("    Facturas  -> dropdown se actualiza solo (formula en H1)")
    print("=" * 72 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

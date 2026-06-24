"""
Pipeline horario secuencial (BD_CONFIG sched_*): cada hora 7:00–00:00 EC.

Orden: ventas+reconciliar → descargo → facturas SRI → (carga MP vía SRI).
A las 00:00 cierra AYER (ventas finales + reconciliar + recalcular stock).
Resto del día: ventas progresivas de HOY + descargo + SRI.

Uso:
  python pipeline_horario.py
  python pipeline_horario.py --dry-run
  python pipeline_horario.py --forzar
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass

ZONA_EC = ZoneInfo("America/Guayaquil")


def _hoy_ec() -> str:
    return datetime.now(ZONA_EC).date().strftime("%Y-%m-%d")


def _hora_ec() -> int:
    return datetime.now(ZONA_EC).hour


def _fecha_operativa(hora: int) -> tuple[str, bool]:
    """
    (fecha, progresivo).
    Medianoche (H00): ayer cerrado, carga normal + reconciliar.
    7–23: hoy en curso, carga progresiva sin reconciliar estricto.
    """
    hoy = datetime.now(ZONA_EC).date()
    if hora == 0:
        ayer = (hoy - timedelta(days=1)).strftime("%Y-%m-%d")
        return ayer, False
    return hoy.strftime("%Y-%m-%d"), True


def _en_ventana_horaria() -> bool:
    from config_sheets import cfg_int

    h = _hora_ec()
    ini = cfg_int("sched_hora_inicio", 7)
    fin = cfg_int("sched_hora_fin", 0)
    if ini <= fin:
        return ini <= h <= fin
    return h >= ini or h <= fin


def main() -> int:
    from config_sheets import cfg
    from estrategia_config import horas_pipeline_sri_descarga, pipeline_sri_solo_proceso
    from pipeline_diario import (
        _checkpoint_complete,
        _checkpoint_start,
        _checkpoint_step_ok,
        run_step,
    )

    ap = argparse.ArgumentParser(description="Pipeline horario Tatami")
    ap.add_argument("--dry-run", action="store_true", help="Solo imprime pasos")
    ap.add_argument("--forzar", action="store_true", help="Ignora ventana horaria")
    args = ap.parse_args()

    hora = _hora_ec()
    if not args.forzar and not _en_ventana_horaria():
        print(f"  INFO: hora {hora} fuera de ventana sched (omitido)")
        return 0

    fecha, progresivo = _fecha_operativa(hora)
    tag = f"horario_{fecha}_H{hora:02d}"
    horario_cp = ROOT / "logs" / "pipeline_horario_checkpoint.json"
    os.environ["PIPELINE_HORARIO_TAG"] = tag

    print("=" * 60)
    print(f"PIPELINE HORARIO — {datetime.now(ZONA_EC):%Y-%m-%d %H:%M} EC")
    print(f"fecha operativa={fecha} | progresivo={progresivo} | tag={tag}")
    print("=" * 60)

    if args.dry_run:
        sri_desc = not pipeline_sri_solo_proceso() and hora in horas_pipeline_sri_descarga()
        print(
            f"  [dry-run] ventas({'progresivo' if progresivo else 'cierre'})"
            f" → {'omit reconciliar' if progresivo else 'reconciliar'}"
            f" → descargo → SRI ({'descarga+proceso' if sri_desc else 'solo-proceso'})"
        )
        if hora == 0:
            print("  [dry-run] + recalcular stock + guardias (cierre medianoche)")
        return 0

    _checkpoint_start(fecha)
    continuar = str(cfg("sched_si_falla_paso", "continuar_con_warn") or "").strip()
    check = continuar != "continuar_con_warn"

    ventas_argv = ["ventas_smartmenu.py", "--fecha", fecha]
    if progresivo:
        ventas_argv.append("--modo-progresivo")

    # 1 Ventas (+ reconciliar si cierre medianoche)
    try:
        run_step(
            f"1/4 — Ventas Smart Menu ({'hoy progresivo' if progresivo else 'cierre ayer'})",
            ventas_argv,
            check=False,
            step=1,
            fecha_objetivo=fecha,
        )
        if progresivo:
            from ventas_completitud import auditar_fecha_remota, mensaje_completitud

            rep_prog = auditar_fecha_remota(fecha)
            if not rep_prog.get("ok") and not rep_prog.get("sin_ventas"):
                print(
                    f"\n  INFO: carga progresiva — {mensaje_completitud(rep_prog)}\n"
                    "  Los tickets nocturnos se completan en el cierre de medianoche (00:00 EC)."
                )
            print("\n  INFO: reconciliar omitido en horario progresivo (día en curso)")
            _checkpoint_step_ok(fecha, 1, "ventas progresivo (sin reconciliar)")
        else:
            rc_rec = run_step(
                "1/4 — Reconciliar ventas (cierre ayer)",
                ["reconciliar_ventas_dia.py", "--fecha", fecha],
                check=False,
                step=1,
                fecha_objetivo=fecha,
            )
            if rc_rec != 0:
                print(
                    "\n  WARN: reconciliar falló — reintentando ventas + reconciliar "
                    "(típico tras carga progresiva diurna incompleta)"
                )
                run_step(
                    "1/4 — Ventas Smart Menu (reintento cierre)",
                    ["ventas_smartmenu.py", "--fecha", fecha],
                    check=False,
                    step=1,
                    fecha_objetivo=fecha,
                )
                rc_rec = run_step(
                    "1/4 — Reconciliar ventas (reintento)",
                    ["reconciliar_ventas_dia.py", "--fecha", fecha],
                    check=True,
                    step=1,
                    fecha_objetivo=fecha,
                )

            from ventas_completitud import (
                asegurar_ventas_dia,
                auditar_fecha_remota,
                dias_con_huecos_recientes,
                mensaje_completitud,
            )

            rep_cierre = auditar_fecha_remota(fecha)
            if not rep_cierre.get("ok") and not rep_cierre.get("sin_ventas"):
                print(f"\n  WARN: huecos tras cierre — {mensaje_completitud(rep_cierre)}")
                rep_fix = asegurar_ventas_dia(fecha)
                if not rep_fix.get("ok"):
                    run_step(
                        "1/4 — Reconciliar ventas (post-reparación)",
                        ["reconciliar_ventas_dia.py", "--fecha", fecha],
                        check=True,
                        step=1,
                        fecha_objetivo=fecha,
                    )
                elif rep_fix.get("reparado"):
                    print(f"  OK: día {fecha} reparado ({rep_fix['grid_docs']} docs)")

            dias_rep = int(os.getenv("VENTAS_BACKFILL_DIAS", "3") or "3")
            for f_hueco in dias_con_huecos_recientes(dias_rep):
                if f_hueco == fecha:
                    continue
                print(f"\n  WARN: hueco detectado en {f_hueco} — reparando...")
                rep_h = asegurar_ventas_dia(f_hueco)
                if rep_h.get("reparado"):
                    print(f"  OK: {f_hueco} reparado")
                elif not rep_h.get("ok"):
                    print(f"  ERROR: {f_hueco} sigue incompleto: {mensaje_completitud(rep_h)}")
                    try:
                        from alertas_tatami import enviar_alerta

                        enviar_alerta(
                            f"Ventas incompletas {f_hueco}",
                            mensaje_completitud(rep_h),
                            estado="ERROR",
                        )
                    except Exception as e:
                        print(f"  WARN: alerta no enviada: {e}")
    except SystemExit as e:
        if check:
            return int(e.code or 1)
        print("  WARN: ventas/reconciliar con error (continúa)")

    # 2 Descargo
    try:
        run_step(
            "2/4 — Descargo inventario",
            ["descargo_inventario.py", "--fecha", fecha],
            check=check,
            step=2,
            fecha_objetivo=fecha,
        )
    except SystemExit as e:
        if check:
            return int(e.code or 1)
        print("  WARN: descargo con error (continúa)")

    # 3 Facturas SRI — descarga en tareas AM/PM; horario solo procesa cola
    sri_argv = ["procesar_facturas_sri.py", "--corrida", f"H{hora:02d}"]
    if pipeline_sri_solo_proceso() or hora not in horas_pipeline_sri_descarga():
        sri_argv.append("--solo-proceso")
    try:
        run_step(
            "3/4 — Facturas SRI",
            sri_argv,
            check=check,
            step=3,
            fecha_objetivo=fecha,
        )
    except SystemExit as e:
        if check:
            return int(e.code or 1)
        print("  WARN: SRI con error (continúa)")

    # 4 Cierre medianoche: recalcular stock del día cerrado
    if hora == 0:
        try:
            run_step(
                "4/5 — Recalcular stock (cierre ayer)",
                ["recalcular_stock_sheets.py", "--produccion"],
                check=False,
                step=4,
                fecha_objetivo=fecha,
            )
            run_step(
                "4b/5 — Guardias costos MP",
                ["guardias_costos_mp.py", "--strict"],
                check=False,
                step=4,
                fecha_objetivo=fecha,
            )
        except SystemExit as e:
            if check:
                return int(e.code or 1)
            print("  WARN: recalcular/guardias con error (continúa)")

    _checkpoint_step_ok(fecha, 4, "pipeline horario OK")
    _checkpoint_complete(fecha)
    horario_cp.parent.mkdir(parents=True, exist_ok=True)
    horario_cp.write_text(
        f'{{"tag":"{tag}","status":"OK","updated":"{datetime.now(ZONA_EC).isoformat()}"}}',
        encoding="utf-8",
    )
    print("\nPipeline horario completado.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"FATAL pipeline_horario: {e}")
        try:
            from pipeline_diario import _alerta_pipeline_fallo

            _alerta_pipeline_fallo(_hoy_ec(), 0, "pipeline_horario", detalle=str(e))
        except Exception:
            pass
        raise SystemExit(1)

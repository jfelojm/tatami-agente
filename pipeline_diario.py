"""
Pipeline diario: ventas -> reconciliacion -> descargo -> recalcular stock -> guardias costos -> PAR/consumo [-> costos teóricos].
Facturas de compra: portal SRI (procesar_facturas_sri.py / tareas TatamiFacturasSRI AM y PM), no Drive.

Candados: tras recalcular_stock corre guardias_costos_mp.py --strict (aborta si costo corrupto).
Cierre mediodia (cuadrante): incluye --with-costos (sync prov, subrecetas, BD_RECETAS, guardias).

Fecha de ventas (sin --fecha): siempre el dia calendario ANTERIOR completo (00:00-23:59)
en America/Guayaquil. Ej.: job el miercoles 12:00 carga martes.

Uso (desde la carpeta tatami-agente, con venv activado o python del venv):
  python pipeline_diario.py
  python pipeline_diario.py --skip-ventas
  python pipeline_diario.py --skip-reconciliar
  python pipeline_diario.py --strict-ventas
  python pipeline_diario.py --fecha 2026-05-11

Checkpoint: logs/pipeline_checkpoint.json (estado RUNNING/OK/FAILED por paso).
Subprocesos: stdout/stderr van al log del Task Scheduler (sin capture_output).
Alertas: import de alertas_tatami al inicio; si falla, WARN visible antes de correr.

Variables: PYTHONIOENCODING=utf-8, RECONCILIAR_TOL_ABS, TATAMI_ALERT_*.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    _dotenv_path = ROOT / ".env"
    if _dotenv_path.is_file():
        load_dotenv(_dotenv_path, override=True)
except ImportError:
    pass
ZONA_EC = ZoneInfo("America/Guayaquil")
CHECKPOINT_PATH = ROOT / "logs" / "pipeline_checkpoint.json"
STALE_RUNNING_MIN = float(os.getenv("PIPELINE_STALE_RUNNING_MIN", "10") or "10")

_alertas_disponibles = False
_alertas_import_error: str | None = None

# Estado de la corrida actual (atexit / señales si el proceso muere a medias)
_pipeline_ctx: dict = {
    "fecha": "",
    "finished": False,
    "alerted": False,
}

try:
    import alertas_tatami  # noqa: F401

    _alertas_disponibles = True
except Exception as e:
    _alertas_import_error = f"{type(e).__name__}: {e}"


def _env() -> dict:
    e = os.environ.copy()
    e.setdefault("PYTHONIOENCODING", "utf-8")
    return e


def _checkpoint_write(data: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {**data, "updated_at": datetime.now(ZONA_EC).isoformat()}
    CHECKPOINT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _checkpoint_read() -> dict | None:
    if not CHECKPOINT_PATH.is_file():
        return None
    try:
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN: checkpoint ilegible ({e})")
        return None


def _checkpoint_warn_previous(fecha_objetivo: str) -> None:
    prev = _checkpoint_read()
    if not prev:
        return
    status = (prev.get("status") or "").upper()
    if status not in ("RUNNING", "FAILED"):
        return
    prev_fecha = (prev.get("fecha_objetivo") or "").strip()
    same_fecha = prev_fecha == fecha_objetivo
    age_min = 9999.0
    try:
        updated = datetime.fromisoformat(prev.get("updated_at", ""))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=ZONA_EC)
        age_min = (datetime.now(ZONA_EC) - updated).total_seconds() / 60
        age_h = age_min / 60
    except (TypeError, ValueError):
        age_h = 999
    if not same_fecha and age_h > 24:
        return
    paso = prev.get("last_ok_step", "?")
    paso_nom = prev.get("last_ok_name") or prev.get("current_step_name") or ""
    err = prev.get("error") or prev.get("failed_step_name") or ""
    print(
        f"\n  *** WARN CHECKPOINT: corrida anterior status={status} "
        f"fecha={prev_fecha} ultimo_paso_ok={paso}"
    )
    if err:
        print(f"      detalle: {err}")
    print("      Revisar logs/ antes de confiar en pasos siguientes.\n")
    if _alertas_disponibles:
        try:
            from alertas_tatami import enviar_alerta

            enviar_alerta(
                "Pipeline: checkpoint previo incompleto",
                f"status={status} fecha={prev_fecha} paso_ok={paso}\n{err}",
                estado="WARN",
            )
            if status == "RUNNING" and age_min >= STALE_RUNNING_MIN:
                from alertas_tatami import alerta_wa_pipeline_fallo

                alerta_wa_pipeline_fallo(
                    prev_fecha or fecha_objetivo,
                    paso_fallo=int(paso) + 1 if str(paso).isdigit() else "?",
                    nombre_paso=paso_nom or "corrida anterior colgada (RUNNING)",
                    detalle=(
                        f"Checkpoint sin cerrar hace {age_min:.0f} min "
                        f"(umbral {STALE_RUNNING_MIN:.0f} min). "
                        "Posible corte en facturas, batería o kill del proceso."
                    ),
                )
        except Exception as e:
            print(f"  WARN: alerta checkpoint: {e}")


def _checkpoint_start(fecha_objetivo: str) -> None:
    _checkpoint_write(
        {
            "fecha_objetivo": fecha_objetivo,
            "status": "RUNNING",
            "last_ok_step": 0,
            "last_ok_name": "inicio",
        }
    )


def _checkpoint_step_ok(fecha_objetivo: str, step: int, name: str) -> None:
    _checkpoint_write(
        {
            "fecha_objetivo": fecha_objetivo,
            "status": "RUNNING",
            "last_ok_step": step,
            "last_ok_name": name,
        }
    )


def _checkpoint_failed(
    fecha_objetivo: str, step: int, name: str, *, code: int | None = None, error: str = ""
) -> None:
    _checkpoint_write(
        {
            "fecha_objetivo": fecha_objetivo,
            "status": "FAILED",
            "failed_step": step,
            "failed_step_name": name,
            "exit_code": code,
            "error": error,
            "last_ok_step": max(0, step - 1),
        }
    )


def _checkpoint_complete(fecha_objetivo: str) -> None:
    _checkpoint_write(
        {
            "fecha_objetivo": fecha_objetivo,
            "status": "OK",
            "last_ok_step": 6,
            "last_ok_name": "pipeline completo",
        }
    )


def _checkpoint_step_in_progress(
    fecha_objetivo: str,
    step: int,
    name: str,
    *,
    last_ok_step: int,
    last_ok_name: str,
) -> None:
    """Marca paso largo en curso (ej. facturas) sin dar por OK el paso aún."""
    _checkpoint_write(
        {
            "fecha_objetivo": fecha_objetivo,
            "status": "RUNNING",
            "last_ok_step": last_ok_step,
            "last_ok_name": last_ok_name,
            "current_step": step,
            "current_step_name": name,
        }
    )


def _alerta_pipeline_fallo(
    fecha_objetivo: str,
    paso: int,
    nombre: str,
    *,
    detalle: str = "",
    codigo: int | None = None,
) -> None:
    if _pipeline_ctx.get("alerted"):
        return
    if not _alertas_disponibles:
        print(
            f"  WARN: alertas_tatami no disponible "
            f"({_alertas_import_error or 'import fallido'})"
        )
        return
    try:
        from alertas_tatami import alerta_wa_pipeline_fallo, enviar_alerta

        enviar_alerta(
            f"Pipeline fallo: paso {paso}",
            f"fecha={fecha_objetivo}\n{nombre}\n{detalle}",
            estado="ERROR",
        )
        alerta_wa_pipeline_fallo(
            fecha_objetivo,
            paso_fallo=paso,
            nombre_paso=nombre,
            detalle=detalle,
            codigo=codigo,
        )
        _pipeline_ctx["alerted"] = True
    except Exception as e:
        print(f"  WARN: alerta pipeline fallo: {e}")


def _pipeline_abort_incomplete(reason: str) -> None:
    """Marca FAILED y avisa si la corrida no terminó en OK."""
    if _pipeline_ctx.get("finished") or _pipeline_ctx.get("alerted"):
        return
    fecha = (_pipeline_ctx.get("fecha") or "").strip()
    if not fecha:
        return
    cp = _checkpoint_read() or {}
    if (cp.get("status") or "").upper() == "OK":
        return
    last_ok = int(cp.get("last_ok_step") or 0)
    cur = cp.get("current_step")
    fail_step = int(cur) if cur is not None else last_ok + 1
    fail_name = (
        cp.get("current_step_name")
        or cp.get("failed_step_name")
        or f"incompleto tras paso {last_ok}"
    )
    _checkpoint_failed(fecha, fail_step, fail_name, error=reason[:500])
    _alerta_pipeline_fallo(fecha, fail_step, fail_name, detalle=reason)


def _pipeline_atexit() -> None:
    _pipeline_abort_incomplete(
        "proceso terminado sin marcar pipeline completo (atexit/kill)"
    )


def _pipeline_signal_handler(signum, frame) -> None:  # noqa: ARG001
    _pipeline_abort_incomplete(f"senal {signum}")
    raise SystemExit(128 + (signum if isinstance(signum, int) else 0))


def _register_pipeline_hooks() -> None:
    atexit.register(_pipeline_atexit)
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _pipeline_signal_handler)
        except (OSError, ValueError):
            pass


def run_step(
    name: str,
    argv: list[str],
    *,
    check: bool = True,
    step: int | None = None,
    fecha_objetivo: str | None = None,
) -> int:
    print("\n" + "=" * 60)
    print(f"PIPELINE: {name}")
    print("=" * 60)
    cmd = [sys.executable, *argv]
    print(f"  $ {' '.join(argv)}\n")
    r = subprocess.run(cmd, cwd=str(ROOT), env=_env())
    if check and r.returncode != 0:
        print(f"\nERROR: paso fallo con codigo {r.returncode}")
        if step is not None and fecha_objetivo:
            _checkpoint_failed(
                fecha_objetivo,
                step,
                name,
                code=r.returncode,
                error=f"subprocess exit {r.returncode}",
            )
            _alerta_pipeline_fallo(
                fecha_objetivo,
                step,
                name,
                detalle=f"subprocess exit {r.returncode}",
                codigo=r.returncode,
            )
        sys.exit(r.returncode)
    if not check and r.returncode != 0:
        print(f"\nWARN: paso termino con codigo {r.returncode} (se continua)")
    if r.returncode == 0 and step is not None and fecha_objetivo:
        _checkpoint_step_ok(fecha_objetivo, step, name)
    if r.returncode == 0:
        try:
            from alertas_pipeline import ping_wa_paso_proceso

            ping_wa_paso_proceso(name)
        except Exception as e:
            print(f"  WARN: ping WA paso: {e}")
    return r.returncode


def _fecha_objetivo_desde_arg(arg_fecha: str | None) -> str:
    if arg_fecha and str(arg_fecha).strip():
        return str(arg_fecha).strip()
    hoy_ec = datetime.now(ZONA_EC).date()
    return (hoy_ec - timedelta(days=1)).strftime("%Y-%m-%d")


def _alertas_inventario_pipeline(origen: str = "pipeline") -> None:
    try:
        from alertas_inventario_barra import enviar_alertas_inventario_barra

        ab = enviar_alertas_inventario_barra(origen=origen)
        if ab.get("enviado"):
            print(
                f"  WA inventario barra: bajo PAR={ab.get('bajo_par')} "
                f"negativos={ab.get('negativos')}"
            )
        elif ab.get("omitido") and ab.get("omitido") not in ("sin alertas",):
            if ab.get("omitido") != "TATAMI_ALERT_INVENTARIO_BARRA no activo":
                print(f"  INFO inventario barra: {ab.get('omitido')}")
    except Exception as e:
        print(f"  WARN: alertas inventario barra: {e}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pipeline diario Tatami (6 pasos)")
    p.add_argument("--skip-ventas", action="store_true", help="Omitir pasos 1 y 2")
    p.add_argument(
        "--skip-reconciliar",
        action="store_true",
        help="Omitir paso 2 (reconciliacion grid vs hist_ventas)",
    )
    p.add_argument(
        "--strict-ventas",
        action="store_true",
        help="Abortar si ventas_smartmenu retorna codigo != 0",
    )
    p.add_argument(
        "--fecha",
        metavar="YYYY-MM-DD",
        default=None,
        help="Fecha ventas/reconcilio (default: ayer en America/Guayaquil)",
    )
    p.add_argument(
        "--solo-ventas",
        action="store_true",
        help="Solo paso 1 (ventas); sin descargo ni inventario. Usar en corrida horaria.",
    )
    p.add_argument(
        "--modo-progresivo",
        action="store_true",
        help="Con --solo-ventas: permite día en curso e incompletitud (sin mover stock).",
    )
    p.add_argument(
        "--modo",
        choices=("completo", "operativo", "nocturno"),
        default="completo",
        help="completo=cierre 12:00 | operativo=ventas+descargo+alertas | nocturno=ayer cuadrado+recalcular",
    )
    p.add_argument(
        "--with-costos",
        action="store_true",
        help="Tras PAR: corregir precio_ref, sync MP, subrecetas y BD_RECETAS (1x/día)",
    )
    return p.parse_args()


def _alerta_strict_ventas_fallo(fecha_objetivo: str, rc: int) -> None:
    if not _alertas_disponibles:
        print(
            f"  WARN: alertas_tatami no disponible "
            f"({_alertas_import_error or 'import fallido'})"
        )
        return
    from alertas_tatami import alerta_wa_ventas_strict_fallo, enviar_alerta

    enviar_alerta(
        "Pipeline: ventas_smartmenu fallo (--strict-ventas)",
        f"fecha={fecha_objetivo} codigo_salida={rc}\n"
        "Revisar log del Programador de tareas o ejecutar ventas a mano.",
        estado="ERROR",
    )
    alerta_wa_ventas_strict_fallo(fecha_objetivo, rc)


def _alerta_pipeline_ok(
    fecha_objetivo: str,
    *,
    resumen_facturas: dict | None = None,
    skip_reconciliar: bool = False,
    notas: list[str] | None = None,
) -> None:
    if not _alertas_disponibles:
        print(
            f"  WARN: alertas_tatami no disponible "
            f"({_alertas_import_error or 'import fallido'})"
        )
        return
    try:
        from alertas_pipeline import enviar_resumen_corrida_horario

        enviar_resumen_corrida_horario(
            fecha_objetivo,
            resumen_facturas=resumen_facturas,
            skip_reconciliar=skip_reconciliar,
            notas=notas,
        )
    except Exception as e:
        print(f"  WARN: enviar_resumen_corrida_horario: {e}")
    from alertas_tatami import alerta_wa_pipeline_ok

    alerta_wa_pipeline_ok(fecha_objetivo)


def main() -> None:
    args = _parse_args()
    if args.modo == "operativo":
        args.modo_progresivo = True
        args.skip_reconciliar = True
    elif args.modo == "nocturno":
        args.skip_reconciliar = False

    fecha_objetivo = _fecha_objetivo_desde_arg(args.fecha)
    hoy_ec = datetime.now(ZONA_EC).date()

    _pipeline_ctx["fecha"] = fecha_objetivo
    _pipeline_ctx["finished"] = False
    _pipeline_ctx["alerted"] = False
    _register_pipeline_hooks()

    print("=" * 60)
    print("PIPELINE DIARIO - Tatami")
    print("=" * 60)
    print(f"  Directorio: {ROOT}")
    print(f"  Python:     {sys.executable}")
    print(f"  Checkpoint: {CHECKPOINT_PATH}")
    print(
        f"  Alertas WA: {'OK' if _alertas_disponibles else 'NO — ' + (_alertas_import_error or '?')}"
    )
    if _alertas_disponibles:
        from alertas_tatami import resumen_config_wa

        print(f"  {resumen_config_wa()}")
    print(f"  Hoy (America/Guayaquil): {hoy_ec}")
    print(f"  Fecha objetivo (ventas + reconcilio): {fecha_objetivo}")
    print(f"  Skip ventas Smart Menu: {args.skip_ventas}")
    print(f"  Skip reconciliacion: {args.skip_reconciliar}")
    print(f"  Ventas estrictas: {args.strict_ventas}")
    print(f"  Solo ventas (horario): {args.solo_ventas}")
    print(f"  Modo pipeline: {args.modo}")
    if args.solo_ventas:
        print(f"  Modo progresivo: {args.modo_progresivo}")

    _checkpoint_warn_previous(fecha_objetivo)
    _checkpoint_start(fecha_objetivo)

    if not args.skip_ventas:
        ventas_argv = ["ventas_smartmenu.py", "--fecha", fecha_objetivo]
        if args.modo_progresivo:
            ventas_argv.append("--modo-progresivo")
        elif args.strict_ventas:
            ventas_argv.append("--strict")
        rc = run_step(
            "1/6 — Ventas Smart Menu -> hist_ventas",
            ventas_argv,
            check=True,
            step=1,
            fecha_objetivo=fecha_objetivo,
        )
        if args.strict_ventas and rc != 0:
            _checkpoint_failed(
                fecha_objetivo,
                1,
                "ventas_smartmenu (--strict)",
                code=rc,
                error="strict-ventas",
            )
            _alerta_strict_ventas_fallo(fecha_objetivo, rc)
            _pipeline_ctx["alerted"] = True
            print(f"\nERROR: ventas termino con codigo {rc} (--strict-ventas)")
            sys.exit(rc)

        if not args.skip_reconciliar and not args.solo_ventas:
            rec_check = args.modo == "nocturno"
            rc_rec = run_step(
                "2/6 — Reconciliar grid Smart Menu vs hist_ventas",
                ["reconciliar_ventas_dia.py", "--fecha", fecha_objetivo],
                check=rec_check,
                step=2,
                fecha_objetivo=fecha_objetivo,
            )
            if rc_rec != 0 and not rec_check:
                print(
                    f"\nWARN: reconciliación falló (codigo {rc_rec}). "
                    "Reintentando ventas + reconciliación una vez..."
                )
                run_step(
                    "1/6 — Ventas Smart Menu -> hist_ventas (reintento)",
                    ventas_argv,
                    check=False,
                    step=1,
                    fecha_objetivo=fecha_objetivo,
                )
                run_step(
                    "2/6 — Reconciliar grid Smart Menu vs hist_ventas (reintento)",
                    ["reconciliar_ventas_dia.py", "--fecha", fecha_objetivo],
                    check=True,
                    step=2,
                    fecha_objetivo=fecha_objetivo,
                )
        else:
            print("\n[2/6] Omitido (--skip-reconciliar) — riesgo: descargo sin cuadre")
            _checkpoint_step_ok(
                fecha_objetivo, 2, "omitido (--skip-reconciliar)"
            )
    else:
        print("\n[1/6] Omitido (--skip-ventas)")
        print("\n[2/6] Omitido (sin ventas nuevas)")
        _checkpoint_step_ok(fecha_objetivo, 1, "omitido (--skip-ventas)")
        _checkpoint_step_ok(fecha_objetivo, 2, "omitido (--skip-ventas)")

    if args.solo_ventas:
        print("\n[2/6] Omitido (--solo-ventas): sin reconciliación en corrida horaria")
        _checkpoint_step_ok(fecha_objetivo, 2, "omitido (--solo-ventas)")
        print("\n[3–6] Omitidos (--solo-ventas): sin descargo, facturas, stock ni PAR")
        _checkpoint_step_ok(fecha_objetivo, 4, "omitido (--solo-ventas)")
        _checkpoint_step_ok(fecha_objetivo, 5, "omitido (--solo-ventas)")
        _checkpoint_step_ok(fecha_objetivo, 6, "omitido (--solo-ventas)")
        _checkpoint_complete(fecha_objetivo)
        _pipeline_ctx["finished"] = True
        print("\nPipeline solo ventas completado.")
        return

    run_step(
        "3/6 — Descargo inventario (hist_ventas -> mov_inventario + stock Sheets)",
        ["descargo_inventario.py", "--fecha", fecha_objetivo],
        step=3,
        fecha_objetivo=fecha_objetivo,
    )

    if args.modo == "operativo":
        print("\n[4–6] Omitidos (modo operativo): sin facturas, recalcular global ni PAR")
        _checkpoint_step_ok(fecha_objetivo, 4, "omitido (modo operativo)")
        _checkpoint_step_ok(fecha_objetivo, 5, "omitido (modo operativo)")
        _checkpoint_step_ok(fecha_objetivo, 6, "omitido (modo operativo)")
        _alertas_inventario_pipeline(origen=f"operativo {fecha_objetivo}")
        _checkpoint_complete(fecha_objetivo)
        _pipeline_ctx["finished"] = True
        print("\nPipeline operativo completado (ventas + descargo + alertas).")
        return

    if args.modo == "nocturno":
        run_step(
            "5/6 — Recalcular stock/costo Sheets (cierre nocturno)",
            ["recalcular_stock_sheets.py", "--produccion"],
            step=5,
            fecha_objetivo=fecha_objetivo,
        )
        run_step(
            "5b — Guardias costos MP (candado post-recalcular)",
            ["guardias_costos_mp.py", "--strict"],
            check=True,
            step=5,
            fecha_objetivo=fecha_objetivo,
        )
        _checkpoint_step_ok(fecha_objetivo, 4, "omitido (modo nocturno)")
        _checkpoint_step_ok(fecha_objetivo, 6, "omitido (modo nocturno)")
        _alertas_inventario_pipeline(origen=f"nocturno {fecha_objetivo}")
        _checkpoint_complete(fecha_objetivo)
        _pipeline_ctx["finished"] = True
        print("\nPipeline nocturno completado (ventas + reconciliar + descargo + recalcular).")
        return

    print("\n" + "=" * 60)
    print(
        "PIPELINE: 4/6 — Facturas compra (omitido; portal SRI TatamiFacturasSRI AM/PM)"
    )
    print("=" * 60)
    print(
        "  INFO: compras vía procesar_facturas_sri.py — "
        "Drive ya no corre en el pipeline diario."
    )
    _checkpoint_step_ok(fecha_objetivo, 4, "omitido (facturas vía SRI)")

    run_step(
        "5/6 — Recalcular stock/costo Sheets desde mov_inventario",
        ["recalcular_stock_sheets.py", "--produccion"],
        step=5,
        fecha_objetivo=fecha_objetivo,
    )
    run_step(
        "5b — Guardias costos MP (candado post-recalcular)",
        ["guardias_costos_mp.py", "--strict"],
        check=True,
        step=5,
        fecha_objetivo=fecha_objetivo,
    )
    from config_sheets import cfg

    if cfg("par_reemplaza_pipeline_diario", False):
        print(
            "\n  INFO: PAR omitido (par semanal domingo — ver par_semanal.py / TatamiPARSemanal)"
        )
        _checkpoint_step_ok(fecha_objetivo, 6, "omitido (PAR semanal)")
    else:
        run_step(
            "6/6 — Calcular PAR y consumo diario en BD_MP_SISTEMA",
            ["calcular_par_levels.py"],
            step=6,
            fecha_objetivo=fecha_objetivo,
        )
    try:
        from alertas_ordenes_compra_barra import enviar_alertas_ordenes_compra_barra

        oc = enviar_alertas_ordenes_compra_barra(origen="pipeline")
        if oc.get("proveedores"):
            print(
                f"  WA órdenes compra barra: {oc.get('proveedores')} proveedores, "
                f"{oc.get('lineas')} líneas"
            )
        elif oc.get("omitido") and oc.get("omitido") not in (
            "sin ítems bajo PAR",
            "TATAMI_ALERT_ORDENES_COMPRA_BARRA no activo",
        ):
            print(f"  INFO órdenes compra barra: {oc.get('omitido')}")
    except Exception as e:
        print(f"  WARN: alertas órdenes compra barra: {e}")

    _alertas_inventario_pipeline(origen="pipeline")

    if args.with_costos:
        run_step(
            "7/7 — Costos teóricos (catálogo → subrecetas → BD_RECETAS)",
            ["recalcular_todos_costos.py", "--produccion"],
            step=7,
            fecha_objetivo=fecha_objetivo,
        )
    else:
        print(
            "\n  INFO: costos teóricos omitidos (usa --with-costos en cierre mediodia)"
        )

    _checkpoint_complete(fecha_objetivo)
    _pipeline_ctx["finished"] = True

    print("\n" + "=" * 60)
    print("PIPELINE DIARIO COMPLETADO")
    print("=" * 60 + "\n")

    _alerta_pipeline_ok(
        fecha_objetivo,
        resumen_facturas=None,
        skip_reconciliar=args.skip_reconciliar,
        notas=[
            "Facturas compra: portal SRI (tareas TatamiFacturasSRI AM/PM).",
            "Revisá el chat con la línea WhatsApp Business del local (Meta), no un contacto personal.",
        ],
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        fecha = (_pipeline_ctx.get("fecha") or "").strip()
        if fecha:
            cp = _checkpoint_read() or {}
            step = int(cp.get("current_step") or cp.get("last_ok_step") or 0) + 1
            nom = cp.get("current_step_name") or type(e).__name__
            _checkpoint_failed(fecha, step, nom, error=str(e)[:500])
            _alerta_pipeline_fallo(fecha, step, nom, detalle=str(e))
        raise

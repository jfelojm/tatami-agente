"""
Digest matutino 8:00 — alertas por área + costos teóricos (BD_CONFIG digest_*, alert_*).

Las alertas WA se envían **antes** de recalcular costos teóricos (evita 429 Sheets
bloqueando bajo PAR / pedidos barra).

Mary: delta costos + pedidos barra (+ SRI aparte en pipeline).
Stock negativo / bajo PAR: jefes + OPS_ALERTAS (Mary pendiente definir — ver BD_CONFIG).

Uso:
  python digest_matutino.py
  python digest_matutino.py --forzar
"""

from __future__ import annotations

import argparse
import os
import subprocess
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


def _env() -> dict:
    e = os.environ.copy()
    e.setdefault("PYTHONIOENCODING", "utf-8")
    return e


def _run_script(script: str, extra: list[str] | None = None) -> int:
    cmd = [sys.executable, str(ROOT / script), *(extra or [])]
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(ROOT), env=_env()).returncode


def _delta_precios_ayer() -> list[dict]:
    from config_sheets import cfg

    if not cfg("alert_delta_costos_activo", True):
        return []
    umbral = float(cfg("alert_delta_costos_umbral", cfg("umbral_alerta_precio", 0.05)) or 0.05)
    ayer = (datetime.now(ZONA_EC).date() - timedelta(days=1)).isoformat()
    hoy = datetime.now(ZONA_EC).date().isoformat()
    try:
        from supabase import create_client

        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        res = (
            sb.table("hist_precios")
            .select(
                "descripcion_proveedor,cod_mp_sistema,nombre_mp,precio_anterior,precio_nuevo,variacion_pct,cod_proveedor,fecha_factura"
            )
            .gte("fecha_factura", ayer)
            .lte("fecha_factura", hoy)
            .execute()
        )
    except Exception as e:
        print(f"  WARN hist_precios: {e}")
        return []

    out = []
    for r in res.data or []:
        var = float(r.get("variacion_pct") or 0)
        if abs(var) <= 1:
            var = abs(var)
        else:
            var = abs(var) / 100.0
        if var >= umbral:
            out.append(r)
    return out


def _negativos_area(bodegas: set[str]) -> list[dict]:
    from whatsapp_webhook import leer_bd_mp_sistema

    rows = leer_bd_mp_sistema()
    out = []
    for r in rows:
        bod = str(r.get("cod_bodega") or "").strip().upper()
        if bod not in bodegas:
            continue
        try:
            stk = float(str(r.get("stock_actual", "0")).replace(",", ".") or 0)
        except ValueError:
            continue
        if stk < 0:
            out.append(
                {
                    "cod_mp_sistema": r.get("cod_mp_sistema"),
                    "nombre_mp": r.get("nombre_mp"),
                    "stock_actual": stk,
                    "cod_bodega": bod,
                    "unidad": r.get("unidad_base"),
                }
            )
    return out


def _bajo_par_area(filtro_mp: set[str] | None) -> list[dict]:
    from inventario_stock_mp import mps_bajo_par
    from whatsapp_webhook import leer_bd_mp_sistema

    rows = leer_bd_mp_sistema()
    out = []
    for cod, info in mps_bajo_par(rows).items():
        if filtro_mp is not None and cod not in filtro_mp:
            continue
        out.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": info.get("nombre_mp"),
                "stock_actual": info.get("stock_total"),
                "par_level": info.get("par_level"),
                "unidad": info.get("unidad_base"),
            }
        )
    return out


def _mps_proveedor_tipo(tipo: str) -> set[str]:
    from alertas_inventario_barra import (
        _norm_cod_mp,
        _norm_cod_prov,
        cargar_proveedores_barra,
    )
    from procesar_facturas_drive import cargar_bd_items_prov

    if tipo.upper() == "BARRA":
        provs = cargar_proveedores_barra()
    else:
        from procesar_facturas_drive import _get_sheet

        from alertas_inventario_barra import _find_header, _row_dict

        sh = _get_sheet()
        vals = sh.worksheet("BD_PROV").get_all_values()
        found = _find_header(vals, "cod_proveedor")
        provs = {}
        if found:
            hi, headers = found
            for row in vals[hi + 1 :]:
                r = _row_dict(headers, row)
                if (r.get("Tipo") or "").strip().upper() == tipo.upper():
                    cp = _norm_cod_prov(r.get("cod_proveedor", ""))
                    if cp:
                        provs[cp] = (r.get("razon_social") or cp).strip()

    mps: set[str] = set()
    for it in cargar_bd_items_prov():
        cp = _norm_cod_prov(it.get("cod_proveedor"))
        if cp in provs:
            mps.add(_norm_cod_mp(it.get("cod_mp_sistema")))
    return {m for m in mps if m}


def _formatear_negativos(items: list[dict]) -> str:
    if not items:
        return "Sin stock negativo."
    lines = [f"⚠ Stock negativo ({len(items)}):"]
    for it in items[:25]:
        lines.append(
            f"- {it.get('nombre_mp')} ({it.get('cod_bodega')}): "
            f"{it.get('stock_actual')} {it.get('unidad')}"
        )
    if len(items) > 25:
        lines.append(f"… y {len(items) - 25} más")
    return "\n".join(lines)


def _formatear_par(items: list[dict]) -> str:
    if not items:
        return "Sin ítems bajo PAR."
    lines = [f"⚠ Bajo PAR ({len(items)}):"]
    for it in items[:25]:
        lines.append(
            f"- {it.get('nombre_mp')}: {it.get('stock_actual')} / "
            f"{it.get('par_level')} {it.get('unidad')}"
        )
    if len(items) > 25:
        lines.append(f"… y {len(items) - 25} más")
    return "\n".join(lines)


def _formatear_delta(items: list[dict]) -> str:
    if not items:
        return "Sin variaciones de costo > umbral."
    lines = [f"📈 Delta costos ({len(items)}):"]
    for r in items[:20]:
        desc = (r.get("descripcion_proveedor") or r.get("nombre_mp") or "?").strip()
        pa = float(r.get("precio_anterior") or 0)
        pn = float(r.get("precio_nuevo") or 0)
        var = float(r.get("variacion_pct") or 0)
        if abs(var) <= 1:
            pct = var * 100
        else:
            pct = var
        lines.append(f"- {desc}: ${pa:.2f} → ${pn:.2f} ({pct:+.1f}%)")
    return "\n".join(lines)


def _enviar_a_roles(clave_roles: str, texto: str, etiqueta: str) -> int:
    from alertas_pipeline import enviar_mensaje_wa
    from estrategia_config import telefonos_alerta

    n = 0
    for tel, lab in telefonos_alerta(clave_roles):
        if enviar_mensaje_wa(tel, texto, etiqueta=f"{etiqueta} {lab}"):
            n += 1
    return n


def _enviar_alertas_wa() -> None:
    """Alertas inventario / pedidos — antes de costos teóricos (Sheets pesado)."""
    from config_sheets import cfg, cfg_tokens
    from estrategia_config import alertas_wa_barra_activas, alertas_wa_cocina_activas

    from alertas_tatami import preview_alertas_activo

    if preview_alertas_activo():
        print("  Modo preview alertas: destino único (TATAMI_ALERTAS_PREVIEW_DESTINO)")

    print("\n--- Alertas WA (prioridad) ---")

    if cfg("alert_pedidos_barra_activo", True):
        try:
            from alertas_ordenes_compra_barra import enviar_alertas_ordenes_compra_barra

            oc = enviar_alertas_ordenes_compra_barra(origen="digest_matutino")
            n_env = int(oc.get("enviados") or 0)
            if n_env:
                print(
                    f"  WA pedidos barra: {oc.get('proveedores')} proveedores, "
                    f"{n_env} mensaje(s) enviado(s)"
                )
            elif oc.get("omitido"):
                print(f"  INFO pedidos barra: {oc.get('omitido')}")
        except Exception as e:
            print(f"  WARN pedidos barra: {e}")

    try:
        from alertas_inventario_barra import enviar_alertas_inventario_barra

        ab = enviar_alertas_inventario_barra(origen="digest_matutino")
        if ab.get("enviado"):
            print(
                f"  WA inventario barra: bajo PAR={ab.get('bajo_par')} "
                f"negativos={ab.get('negativos')}"
            )
        elif ab.get("omitido") and ab.get("omitido") != "sin alertas":
            print(f"  INFO inventario barra: {ab.get('omitido')}")
    except Exception as e:
        print(f"  WARN inventario barra: {e}")

    bod_barra = cfg_tokens("area_barra_bodegas", {"BOD-002", "BOD-003"})
    bod_cocina = cfg_tokens("area_cocina_bodegas", {"BOD-001", "BOD-005"})
    mps_barra = _mps_proveedor_tipo("Barra")
    mps_cocina = _mps_proveedor_tipo("Cocina")
    deltas = _delta_precios_ayer()
    fecha = datetime.now(ZONA_EC).strftime("%Y-%m-%d")

    areas: list[str] = []
    if alertas_wa_barra_activas():
        areas.append("barra")
    if alertas_wa_cocina_activas():
        areas.append("cocina")
    if not areas:
        print("  INFO: sin areas con alertas WA activas")
        return

    for area in areas:
        neg_partes: list[str] = []
        par_partes: list[str] = []
        if cfg("alert_stock_negativo_activo", True):
            bod = set(bod_barra if area == "barra" else bod_cocina)
            neg = _negativos_area(bod)
            if neg:
                neg_partes.append(_formatear_negativos(neg))
        if cfg("alert_bajo_par_activo", True):
            mps = mps_barra if area == "barra" else mps_cocina
            par = _bajo_par_area(mps or None)
            if par:
                par_partes.append(_formatear_par(par))
        if neg_partes:
            texto_neg = f"📋 *Stock negativo {area.upper()}* — {fecha}\n\n" + "\n\n".join(
                neg_partes
            )
            n = _enviar_a_roles(
                f"alert_stock_negativo_roles_{area}",
                texto_neg[:4000],
                f"digest negativos {area}",
            )
            print(f"  digest negativos {area}: {n} destinatario(s)")
        if par_partes:
            texto_par = f"📋 *Inventario {area.upper()}* — {fecha}\n\n" + "\n\n".join(
                par_partes
            )
            n = _enviar_a_roles(
                f"alert_bajo_par_roles_{area}",
                texto_par[:4000],
                f"digest ops {area}",
            )
            print(f"  digest ops {area}: {n} destinatario(s)")

        if cfg("alert_delta_costos_activo", True) and deltas:
            texto_delta = f"📈 *Delta costos {area.upper()}* — {fecha}\n\n" + _formatear_delta(deltas)
            n = _enviar_a_roles(
                f"alert_delta_costos_roles_{area}",
                texto_delta[:4000],
                f"digest delta {area}",
            )
            print(f"  digest delta {area}: {n} destinatario(s)")


def main() -> int:
    from config_sheets import cfg

    ap = argparse.ArgumentParser(description="Digest matutino Tatami")
    ap.add_argument(
        "--forzar",
        action="store_true",
        help="Ignora deduplicación del día (repetir digest ya completado)",
    )
    args = ap.parse_args()

    if not cfg("digest_matutino_activo", True):
        print("  INFO: digest_matutino_activo=false")
        return 0

    from config_sheets import cfg_int
    from pipeline_run_guard import corrida_fuera_de_tolerancia

    hora_digest = 8
    raw_h = str(cfg("digest_matutino_hora", "8:00") or "8:00").strip()
    if ":" in raw_h:
        try:
            hora_digest = int(raw_h.split(":")[0]) % 24
        except ValueError:
            pass
    else:
        try:
            hora_digest = int(raw_h) % 24
        except ValueError:
            pass
    tol = max(1, min(cfg_int("sched_tolerancia_min", 8), 30))
    if not args.forzar:
        tarde, motivo = corrida_fuera_de_tolerancia(
            hora_esperada=hora_digest, minuto_esperado=0, tolerancia_min=tol
        )
        if tarde:
            print(
                f"  INFO: digest omitido — fuera de ventana programada ({motivo}). "
                f"Esperado ~{hora_digest:02d}:00 EC."
            )
            return 0

    fecha = datetime.now(ZONA_EC).date().strftime("%Y-%m-%d")
    slot = f"digest_{fecha}"

    from pipeline_run_guard import corrida_unica, marcar_slot_completado

    with corrida_unica(
        "digest_matutino",
        slot_id=slot,
        slots_name="digest_matutino",
        forzar=args.forzar,
        lock_ttl_min=120,
    ) as ejecutar:
        if not ejecutar:
            return 0

        print("=" * 60)
        print(f"DIGEST MATUTINO — {datetime.now(ZONA_EC):%Y-%m-%d %H:%M} EC")
        print("=" * 60)

        _enviar_alertas_wa()

        if cfg("pipe_costos_activo", True):
            print("\n--- Costos teóricos (post-alertas) ---")
            rc = _run_script(
                str(cfg("pipe_costos_script", "recalcular_todos_costos.py")),
                ["--produccion"],
            )
            if rc != 0:
                print(f"  WARN: costos teóricos exit {rc}")
                return rc
            try:
                from alertas_pipeline import ping_wa_paso_proceso

                ping_wa_paso_proceso("Costos teóricos (digest 8:00)")
            except Exception as e:
                print(f"  WARN: ping WA costos: {e}")

        marcar_slot_completado("digest_matutino", slot)
        print("Digest matutino finalizado.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

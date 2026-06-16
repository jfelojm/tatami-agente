"""
Descarga comprobantes recibidos del SRI y los procesa con el pipeline de facturas.

Corridas programadas: 10:00 (AM) y 18:00 (PM) vía Task Scheduler + ejecutar_facturas_sri.ps1

Uso:
  python procesar_facturas_sri.py --corrida AM
  python procesar_facturas_sri.py --corrida PM --dry-run
  python procesar_facturas_sri.py --init-portal-session
  python procesar_facturas_sri.py --solo-descarga
  python procesar_facturas_sri.py --solo-proceso
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)

from procesar_facturas_drive import (
    factura_ya_procesada,
    parsear_xml_sri,
    procesar_factura_dict,
    registrar_factura_procesada,
)
from sri_client import (
    ComprobanteRecibido,
    SriConfig,
    SriPortalClient,
    SriSoapClient,
    metadata_desde_xml,
    ventana_fechas,
)


def _supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Faltan SUPABASE_URL / SUPABASE_KEY en .env")
    return create_client(url, key)


def _fecha_iso(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10] if len(s) >= 10 else None


def clave_ya_registrada(sb, clave: str) -> dict | None:
    try:
        res = (
            sb.table("sri_comprobantes_recibidos")
            .select("*")
            .eq("clave_acceso", clave)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    except Exception as e:
        err = str(e).lower()
        if "sri_comprobantes_recibidos" in err or "does not exist" in err:
            raise RuntimeError(
                "Tabla sri_comprobantes_recibidos no existe. "
                "Ejecute sql/add_sri_comprobantes_recibidos.sql en Supabase."
            ) from e
        print(f"  WARN consultando clave {clave[:8]}...: {e}")
    return None


def guardar_descarga(
    sb,
    comp: ComprobanteRecibido,
    xml: str,
    meta_extra: dict,
    dry_run: bool = False,
) -> None:
    meta = dict(meta_extra)
    meta.setdefault("origen", "SRI")
    md = metadata_desde_xml(xml)
    num = comp.num_factura or md.get("num_factura") or ""
    ruc = comp.ruc_emisor or md.get("ruc_emisor") or ""
    razon = comp.razon_social or md.get("razon_social") or ""
    fecha = _fecha_iso(comp.fecha_emision or md.get("fecha_emision") or "")

    registro = {
        "clave_acceso": comp.clave_acceso,
        "num_factura": num,
        "ruc_emisor": ruc,
        "razon_social": razon,
        "fecha_emision": fecha,
        "xml_autorizado": xml,
        "fecha_descarga": datetime.now().isoformat(),
        "estado": "DESCARGADO",
        "meta": meta,
    }
    if dry_run:
        print(f"  [DRY RUN] guardaría clave {comp.clave_acceso[:12]}... ({num})")
        return
    sb.table("sri_comprobantes_recibidos").upsert(
        registro, on_conflict="clave_acceso"
    ).execute()


def marcar_estado(sb, clave: str, estado: str, meta_patch: dict | None = None, dry_run: bool = False):
    if dry_run:
        print(f"  [DRY RUN] estado {clave[:12]}... -> {estado}")
        return
    payload: dict = {
        "estado": estado,
        "fecha_proceso": datetime.now().isoformat(),
    }
    if meta_patch:
        existente = clave_ya_registrada(sb, clave) or {}
        meta = dict(existente.get("meta") or {})
        meta.update(meta_patch)
        payload["meta"] = meta
    sb.table("sri_comprobantes_recibidos").update(payload).eq(
        "clave_acceso", clave
    ).execute()


def _estado_cola_sri(sb) -> dict:
    """Conteos DESCARGADO / ERROR aún pendientes en Supabase."""
    try:
        rows = (
            sb.table("sri_comprobantes_recibidos")
            .select("estado")
            .execute()
            .data
            or []
        )
        from collections import Counter

        c = Counter(r.get("estado") for r in rows)
        return {
            "descargado": c.get("DESCARGADO", 0),
            "error": c.get("ERROR", 0),
        }
    except Exception:
        return {"descargado": 0, "error": 0}


def listar_pendientes_proceso(sb) -> list[dict]:
    """Todos los XML DESCARGADO en Supabase (sin filtro de ventana)."""
    try:
        res = (
            sb.table("sri_comprobantes_recibidos")
            .select("*")
            .eq("estado", "DESCARGADO")
            .order("fecha_descarga")
            .execute()
        )
        return res.data or []
    except Exception as e:
        err = str(e).lower()
        if "sri_comprobantes_recibidos" in err or "does not exist" in err or "pgrst205" in err:
            raise RuntimeError(
                "Tabla sri_comprobantes_recibidos no existe. "
                "Ejecute sql/add_sri_comprobantes_recibidos.sql en Supabase SQL Editor."
            ) from e
        raise RuntimeError(f"Error listando pendientes SRI: {e}") from e


def fase_descarga(
    config: SriConfig,
    corrida: str,
    dry_run: bool,
    fecha_desde: date,
    fecha_hasta: date,
) -> dict:
    print(f"\n{'=' * 60}")
    print(f"FASE DESCARGA SRI - corrida {corrida}")
    print(f"Ventana: {fecha_desde} -> {fecha_hasta}")
    print(f"{'=' * 60}")

    portal = SriPortalClient(config)
    soap = SriSoapClient(config)
    sb = _supabase()

    print("Consultando portal (comprobantes recibidos)...")
    try:
        listados = portal.listar_recibidos(fecha_desde, fecha_hasta)
    except Exception as e:
        print(f"ERROR portal SRI: {e}")
        raise

    print(f"Comprobantes listados en portal: {len(listados)}")
    vacio = {
        "ejecutada": True,
        "listados": len(listados),
        "descargados": 0,
        "omitidos": 0,
        "errores": 0,
        "errores_detalle": [],
    }
    if not listados:
        return vacio

    meta_corrida = {"corrida": corrida, "origen": "SRI", "fase": "descarga"}
    descargados = 0
    omitidos = 0
    errores = 0
    errores_detalle: list[dict] = []

    for i, comp in enumerate(listados, 1):
        clave = comp.clave_acceso
        previo = clave_ya_registrada(sb, clave)
        if previo and previo.get("xml_autorizado") and previo.get("estado") != "ERROR":
            omitidos += 1
            print(f"  [{i}/{len(listados)}] SKIP clave ya descargada: {clave[:12]}...")
            continue

        print(f"  [{i}/{len(listados)}] Descargando {clave[:12]}...")
        try:
            xml = soap.descargar_xml_autorizado(clave)
            guardar_descarga(sb, comp, xml, meta_corrida, dry_run=dry_run)
            descargados += 1
        except Exception as e:
            errores += 1
            print(f"    ERROR descarga SOAP: {e}")
            errores_detalle.append(
                {
                    "num_factura": comp.num_factura or "",
                    "clave": clave[:12],
                    "error": str(e),
                }
            )
            if not dry_run:
                try:
                    sb.table("sri_comprobantes_recibidos").upsert(
                        {
                            "clave_acceso": clave,
                            "num_factura": comp.num_factura or "",
                            "ruc_emisor": comp.ruc_emisor or "",
                            "razon_social": comp.razon_social or "",
                            "fecha_emision": _fecha_iso(comp.fecha_emision),
                            "estado": "ERROR",
                            "fecha_descarga": datetime.now().isoformat(),
                            "meta": {**meta_corrida, "error_descarga": str(e)},
                        },
                        on_conflict="clave_acceso",
                    ).execute()
                except Exception as e2:
                    print(f"    WARN no se pudo registrar error: {e2}")

    print(
        f"\nResumen descarga: {descargados} nuevos, {omitidos} ya en DB, {errores} errores"
    )
    return {
        "ejecutada": True,
        "listados": len(listados),
        "descargados": descargados,
        "omitidos": omitidos,
        "errores": errores,
        "errores_detalle": errores_detalle,
    }


def fase_proceso(
    config: SriConfig,
    corrida: str,
    dry_run: bool,
) -> dict:
    print(f"\n{'=' * 60}")
    print(f"FASE PROCESO SRI - corrida {corrida}")
    print(f"{'=' * 60}")

    sb = _supabase()
    pendientes = listar_pendientes_proceso(sb)
    print(f"Comprobantes pendientes de procesar: {len(pendientes)}")

    meta_corrida = {"corrida": corrida, "origen": "SRI", "fase": "proceso"}
    ok = parcial = omitido = err = 0
    sin_match_corrida: list[dict] = []

    for i, row in enumerate(pendientes, 1):
        clave = row["clave_acceso"]
        xml = row.get("xml_autorizado") or ""
        num_prev = (row.get("num_factura") or "").strip()
        print(f"\n  [{i}/{len(pendientes)}] Procesando {num_prev or clave[:12]}...")

        if not xml.strip():
            print("    ERROR: sin xml_autorizado en DB")
            marcar_estado(sb, clave, "ERROR", {"error": "sin_xml"}, dry_run)
            err += 1
            continue

        factura = parsear_xml_sri(xml)
        if not factura:
            print("    ERROR: parsear_xml_sri falló")
            marcar_estado(sb, clave, "ERROR", {"error": "parseo_xml"}, dry_run)
            err += 1
            continue

        factura["_meta"] = {
            **meta_corrida,
            "clave_acceso": clave,
            "origen_descarga": "SRI",
        }
        factura["_archivo_drive"] = {"id": "", "name": f"SRI_{clave}.xml"}

        num = factura["num_factura"]
        ruc = factura["ruc"]
        if not dry_run and factura_ya_procesada(num, ruc):
            print(f"    OMITIDO: ya en facturas_procesadas ({num})")
            marcar_estado(sb, clave, "OMITIDO", {"num_factura": num}, dry_run)
            omitido += 1
            continue

        resultado = procesar_factura_dict(factura, dry_run=dry_run, origen="XML")
        items_mat = int(resultado.get("matcheados") or 0)
        sin_match = resultado.get("sin_match") or []
        items_warn = len(resultado.get("warn") or []) + len(sin_match)
        estado = (resultado.get("estado") or "PARCIAL").strip().upper()
        for sm in sin_match:
            if isinstance(sm, dict):
                sin_match_corrida.append(
                    {
                        "factura": num,
                        "descripcion": sm.get("descripcion") or "",
                        "estado": sm.get("estado") or "",
                    }
                )

        registrar_factura_procesada(
            factura,
            {"id": ""},
            items_mat,
            items_warn,
            dry_run=dry_run,
        )

        if not dry_run:
            try:
                sb.table("facturas_procesadas").update(
                    {"meta": {**(factura.get("_meta") or {}), "origen": "SRI", "clave_acceso": clave}}
                ).eq("num_factura", num).eq("ruc_proveedor", ruc).execute()
            except Exception:
                pass

        marcar_estado(
            sb,
            clave,
            "PROCESADO",
            {"estado_factura": estado, "items_matcheados": items_mat},
            dry_run=dry_run,
        )

        if estado == "COMPLETA":
            ok += 1
        else:
            parcial += 1

    print(
        f"\nResumen proceso: {ok} completas, {parcial} parciales, "
        f"{omitido} omitidas, {err} errores"
    )
    return {
        "ejecutada": True,
        "pendientes_ini": len(pendientes),
        "completas": ok,
        "parciales": parcial,
        "omitidas": omitido,
        "errores": err,
        "sin_match": sin_match_corrida,
    }


def init_portal_session(config: SriConfig) -> None:
    config.portal_headless = False
    portal = SriPortalClient(config)
    portal.guardar_sesion_interactiva()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descarga y procesa facturas recibidas SRI")
    parser.add_argument(
        "--corrida",
        default=os.getenv("SRI_CORRIDA", "AM"),
        help="Etiqueta de corrida (AM, PM, H07, etc.)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Sin escrituras en Supabase/Sheets")
    parser.add_argument(
        "--solo-descarga",
        action="store_true",
        help="Solo listar portal + descargar XML a Supabase",
    )
    parser.add_argument(
        "--solo-proceso",
        action="store_true",
        help="Solo procesar filas DESCARGADO en Supabase",
    )
    parser.add_argument(
        "--init-portal-session",
        action="store_true",
        help="Login manual en navegador; guarda sesión para corridas headless",
    )
    args = parser.parse_args(argv)

    config = SriConfig.from_env()
    faltantes = config.validar()
    if faltantes and not args.init_portal_session:
        print(f"ERROR: faltan variables en .env: {', '.join(faltantes)}")
        return 1

    print("=" * 60)
    print(f"Tatami - Facturas SRI | {datetime.now().strftime('%Y-%m-%d %H:%M')} | corrida {args.corrida}")
    fecha_desde, fecha_hasta = ventana_fechas(config.ventana_dias)
    print(
        f"Ambiente SOAP: {config.ambiente} | ventana descarga: "
        f"{fecha_desde} .. {fecha_hasta} ({config.ventana_dias} dias previos + hoy)"
    )
    if args.dry_run:
        print("MODO DRY-RUN (sin persistir)")
    print("=" * 60)

    if args.init_portal_session:
        init_portal_session(config)
        return 0

    resumen_desc: dict = {"ejecutada": False}
    resumen_proc: dict = {"ejecutada": False}
    fatal_error: str | None = None
    exit_code = 0

    try:
        if not args.solo_proceso:
            resumen_desc = fase_descarga(
                config,
                args.corrida,
                args.dry_run,
                fecha_desde,
                fecha_hasta,
            )
        if not args.solo_descarga:
            resumen_proc = fase_proceso(config, args.corrida, args.dry_run)
    except Exception as e:
        fatal_error = str(e)
        print(f"\nFATAL: {e}")
        exit_code = 1
    finally:
        if not args.dry_run:
            cola = {"descargado": 0, "error": 0}
            try:
                cola = _estado_cola_sri(_supabase())
            except Exception as e:
                print(f"  WARN cola SRI para aviso: {e}")
            try:
                from alertas_pipeline import enviar_resumen_facturas_sri

                enviar_resumen_facturas_sri(
                    {
                        "corrida": args.corrida,
                        "ventana": f"{fecha_desde} .. {fecha_hasta}",
                        "descarga": resumen_desc,
                        "proceso": resumen_proc,
                        "cola": cola,
                    },
                    fatal_error=fatal_error,
                )
            except Exception as e:
                print(f"  WARN aviso WhatsApp SRI: {e}")

    if exit_code:
        return exit_code

    print("\nCorrida SRI finalizada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Operaciones de conteo físico para WhatsApp y otros clientes (sin CLI).

Flujo estándar: crear ciclo → snapshot → plantilla Sheets → captura → enviar (Apps Script / API).
"""

from __future__ import annotations

from datetime import date

from conteo_fisico import (
    ConteoOperacionError,
    crear_ciclo_api,
    listar_ciclos_api,
    snapshot_ciclo_api,
    anular_ciclo_api,
)
from plantilla_conteo_sheets import generar_plantilla_desde_ciclo


def sheet_name_por_bodega(cod_bodega: str) -> str:
    cod = (cod_bodega or "").strip().upper()
    if cod == "BOD-002":
        return "CONTEO_BARRA"
    return "CONTEO"


def semana_iso_actual() -> tuple[int, int]:
    iso = date.today().isocalendar()
    return int(iso[0]), int(iso[1])


def iniciar_conteo_wa(
    cod_bodega: str,
    *,
    anio: int | None = None,
    semana_iso: int | None = None,
    sheet_name: str | None = None,
    reemplazar_snapshot: bool = False,
    sobreescribir_hoja: bool = True,
    responsable_nombre: str | None = None,
    responsable_contacto: str | None = None,
    notas: str | None = None,
) -> dict:
    """
    Inicia un ciclo de conteo listo para captura en Sheets.
    Equivale a: crear-ciclo + snapshot + plantilla_conteo_sheets --desde-ciclo-id.
    """
    cod_bodega = (cod_bodega or "").strip()
    if not cod_bodega:
        raise ConteoOperacionError("VALIDATION", "cod_bodega es obligatorio (ej. BOD-001, BOD-002)")

    if not anio or not semana_iso:
        ay, aw = semana_iso_actual()
        anio = anio or ay
        semana_iso = semana_iso or aw

    hoja = (sheet_name or sheet_name_por_bodega(cod_bodega)).strip()

    ciclo = crear_ciclo_api(
        anio=int(anio),
        semana_iso=int(semana_iso),
        cod_bodega=cod_bodega,
        sheet_name=hoja,
        responsable_nombre=responsable_nombre,
        responsable_contacto=responsable_contacto,
        notas=notas,
    )
    ciclo_id = str(ciclo.get("id", "")).strip()

    snap = snapshot_ciclo_api(ciclo_id, reemplazar=reemplazar_snapshot)
    plantilla = generar_plantilla_desde_ciclo(
        ciclo_id, hoja, sobreescribir=sobreescribir_hoja
    )

    return {
        "ok": True,
        "ciclo_id": ciclo_id,
        "cod_bodega": cod_bodega,
        "anio": anio,
        "semana_iso": semana_iso,
        "sheet_name": hoja,
        "estado": snap.get("estado"),
        "mps_en_snapshot": snap.get("lineas_insertadas"),
        "spreadsheet_id": plantilla.get("spreadsheet_id"),
        "url_hoja": plantilla.get("url_hoja"),
        "instrucciones": [
            f"Abrir la hoja '{hoja}' en el maestro de datos.",
            f"Verificar ciclo_id en celda B2: {ciclo_id}",
            "Rellenar columna G (conteo_fisico) en todas las filas; 0 es válido.",
            "Menú Conteo → Enviar a Tatami (requiere Apps Script y TATAMI_CONTEO_API_URL configurados).",
            "Tras enviar, Moisés puede aprobar por WhatsApp (APROBAR TODO / APROBAR nombre).",
        ],
    }


def resumen_ciclos_abiertos() -> dict:
    abiertos = listar_ciclos_api(limit=20)
    abiertos = [
        c
        for c in abiertos
        if (c.get("estado") or "") not in ("CONTABILIZADO", "ANULADO")
    ]
    return {
        "total": len(abiertos),
        "ciclos": [
            {
                "ciclo_id": c.get("id"),
                "estado": c.get("estado"),
                "cod_bodega": c.get("cod_bodega"),
                "semana_iso": c.get("semana_iso"),
                "anio": c.get("anio"),
                "sheet_name": c.get("sheet_name"),
            }
            for c in abiertos
        ],
    }

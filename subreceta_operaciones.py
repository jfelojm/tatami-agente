"""
Producción de subrecetas vía WhatsApp / API interna.

Eduardo (barra) registra preparación: baja MPs y entra semi en BOD-002.
"""

from __future__ import annotations

from registrar_produccion_subreceta import (
    _abrir_maestro,
    _cargar_mapa_stock,
    _norm_sub,
    _subs_meta_desde_cab,
    planificar_produccion,
    registrar,
)
from subrecetas_detalle import agrupar_detalle_por_padre, cargar_bd_subrecetas, cargar_bd_subrecetas_detalle
from calcular_costo_subrecetas import cargar_costos_mp


class SubrecetaOperacionError(Exception):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def _formatear_plan_wa(plan: dict, *, simular: bool) -> str:
    lines = [
        f"{'[SIMULACIÓN] ' if simular else ''}Producción SUB {plan['cod_subreceta']} — {plan['nombre_subreceta']}",
        f"Lote: {plan['cantidad_producida']} {plan['unidad']} (rend {plan['rendimiento_estandar']}, factor {plan['factor']})",
        f"Entrada: {plan['entrada_sub']['cod_mp_sistema']} +{plan['entrada_sub']['cantidad_mov']} {plan['entrada_sub']['unidad_base']} @ {plan['bodega_destino']}",
        "Salidas MP:",
    ]
    for s in plan["salidas_mp"][:12]:
        lines.append(
            f"  • {s['cod_mp_sistema']} @ {s['cod_bodega']}: -{s['cantidad_mov']} {s['unidad_base']}"
        )
    if len(plan["salidas_mp"]) > 12:
        lines.append(f"  … +{len(plan['salidas_mp']) - 12} líneas más")
    if plan["avisos"]:
        lines.append("Avisos:")
        for a in plan["avisos"][:6]:
            lines.append(f"  ! {a}")
    if simular:
        lines.append("Para aplicar: repite el comando con CONFIRMAR al final.")
    return "\n".join(lines)


def producir_subreceta_wa(
    codigos: list[str],
    *,
    bodega: str = "BOD-002",
    cantidad: float | None = None,
    registrado_por: str = "WhatsApp",
    simular: bool = True,
    forzar: bool = False,
    recalcular: bool = True,
) -> dict:
    """
    Una o varias subrecetas (051 052 …). simular=True solo muestra el plan (evaluación).
    """
    if not codigos:
        raise SubrecetaOperacionError("VALIDATION", "Indica al menos un cod_subreceta (ej. 051)")

    bodega = (bodega or "BOD-002").strip().upper()
    sh = _abrir_maestro()
    cab = cargar_bd_subrecetas(sh)
    por_padre = agrupar_detalle_por_padre(cargar_bd_subrecetas_detalle(sh))
    costos_mp = cargar_costos_mp(sh)
    subs_meta = _subs_meta_desde_cab(cab)
    stock_map = _cargar_mapa_stock(sh)

    planes: list[dict] = []
    resultados: list[dict] = []
    textos: list[str] = []
    omitidos: list[str] = []

    for raw in codigos:
        cod = _norm_sub(raw)
        try:
            plan = planificar_produccion(
                cod,
                cantidad_producida=cantidad,
                bodega_destino=bodega,
                sh=sh,
                cab=cab,
                por_padre=por_padre,
                costos_mp=costos_mp,
                subs_meta=subs_meta,
                stock_map=stock_map,
            )
            planes.append(plan)
            textos.append(_formatear_plan_wa(plan, simular=simular))

            if plan["avisos"] and not forzar and not simular:
                omitidos.append(cod)
                continue

            if not simular:
                res = registrar(
                    plan, produccion=True, registrado_por=registrado_por
                )
                resultados.append(res)
        except ValueError as e:
            textos.append(f"SUB {raw}: error — {e}")
            omitidos.append(cod)

    if not simular and recalcular and resultados:
        import os
        import subprocess
        import sys

        subprocess.run(
            [sys.executable, "recalcular_stock_sheets.py", "--produccion"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            check=False,
        )

    return {
        "ok": len(omitidos) == 0 or simular,
        "simular": simular,
        "bodega": bodega,
        "producidas": [r.get("cod_subreceta") for r in resultados],
        "omitidas": omitidos,
        "texto_whatsapp": "\n\n".join(textos),
        "planes": [
            {
                "cod_subreceta": p["cod_subreceta"],
                "cantidad": p["cantidad_producida"],
                "unidad": p["unidad"],
                "avisos": p["avisos"],
            }
            for p in planes
        ],
        "documentos": [r.get("documento") for r in resultados],
    }

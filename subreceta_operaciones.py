"""
Producción de subrecetas vía WhatsApp / API interna.

Eduardo (barra) registra preparación: baja MPs y entra semi en BOD-002.
"""

from __future__ import annotations

from registrar_produccion_subreceta import (
    _abrir_maestro,
    _cargar_mapa_nombres_mp,
    _cargar_mapa_stock,
    _norm_sub,
    _resolver_nombre_mp,
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


def _cod_sub_display(cod: str) -> str:
    c = (cod or "").strip().upper()
    if c.startswith("SUB-"):
        return c
    digits = c.replace("SUB-", "").strip()
    if digits.isdigit():
        return f"SUB-{digits.zfill(3)}"
    return c


def _lookup_nombre_mp(cod: str, nombres_mp: dict[str, str] | None) -> str:
    if not cod or not nombres_mp:
        return ""
    cod = cod.strip()
    if cod in nombres_mp:
        return (nombres_mp[cod] or "").strip()
    bare = cod.lstrip("0") or cod
    for k, v in nombres_mp.items():
        if not v:
            continue
        kb = (k or "").strip().lstrip("0") or k
        if kb == bare or k.strip() == cod:
            return v.strip()
    return ""


def _etiqueta_sub(cod: str, nombre: str = "") -> str:
    c = _cod_sub_display(cod)
    n = (nombre or "").strip()
    if n and n.upper() != c and not n.upper().startswith("SUB-"):
        return f"{n} ({c})"
    return c


def _etiqueta_mp(
    item: dict,
    nombres_mp: dict[str, str] | None = None,
    subs_meta: dict[str, dict] | None = None,
) -> str:
    cod = (item.get("cod_mp_sistema") or "").strip()
    nom = (item.get("nombre_mp") or "").strip()
    if not nom or nom == cod or (nom.isdigit() and nom.lstrip("0") == cod.lstrip("0")):
        nom = _resolver_nombre_mp(
            cod,
            nombre_detalle=nom,
            nombres_mp=nombres_mp,
            subs_meta=subs_meta,
        )
    if not nom or nom == cod:
        nom = _lookup_nombre_mp(cod, nombres_mp)
    if str(cod).upper().startswith("SUB-"):
        return _etiqueta_sub(cod, nom)
    if nom and nom != cod:
        return f"{nom} ({cod})"
    return cod or "?"


def _formatear_plan_wa(
    plan: dict,
    *,
    simular: bool,
    nombres_mp: dict[str, str] | None = None,
    subs_meta: dict[str, dict] | None = None,
    omitir_avisos_stock: bool = False,
) -> str:
    ctx = {"nombres_mp": nombres_mp, "subs_meta": subs_meta}
    etiqueta = lambda item: _etiqueta_mp(item, **ctx)
    lines = [
        f"{'[SIMULACIÓN] ' if simular else ''}Producción {_etiqueta_sub(plan['cod_subreceta'], plan['nombre_subreceta'])}",
        f"Lote: {plan['cantidad_producida']} {plan['unidad']} (rend {plan['rendimiento_estandar']}, factor {plan['factor']})",
        f"Entrada: {etiqueta(plan['entrada_sub'])} +{plan['entrada_sub']['cantidad_mov']} {plan['entrada_sub']['unidad_base']} @ {plan['bodega_destino']}",
        "Salidas MP:",
    ]
    for s in plan["salidas_mp"][:12]:
        lines.append(
            f"  • {etiqueta(s)} @ {s['cod_bodega']}: -{s['cantidad_mov']} {s['unidad_base']}"
        )
    if len(plan["salidas_mp"]) > 12:
        lines.append(f"  … +{len(plan['salidas_mp']) - 12} líneas más")
    if plan["avisos"]:
        avisos_plan = list(plan["avisos"])
        if omitir_avisos_stock:
            from estrategia_config import filtrar_avisos_stock_produccion

            stock_set = set(filtrar_avisos_stock_produccion(avisos_plan))
            avisos_plan = [a for a in avisos_plan if a not in stock_set]
        if avisos_plan:
            lines.append("Avisos:")
            for a in avisos_plan[:8]:
                lines.append(f"  ! {a}")
    if simular:
        lines.append("Para aplicar: responde *SÍ* o *confirmo*.")
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
    omitir_avisos_stock: bool = False,
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
    nombres_mp = _cargar_mapa_nombres_mp(sh)

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
            textos.append(
                _formatear_plan_wa(
                    plan,
                    simular=simular,
                    nombres_mp=nombres_mp,
                    subs_meta=subs_meta,
                    omitir_avisos_stock=omitir_avisos_stock,
                )
            )

            if plan["avisos"] and not forzar and not simular:
                from estrategia_config import aviso_produccion_bloquea_registro

                if any(aviso_produccion_bloquea_registro(a) for a in plan["avisos"]):
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

"""
Roles, permisos y destinatarios WA desde BD_CONFIG + allowlists en .env.

Fuente de verdad operativa: BD_CONFIG (claves perm_*, rol_*, alert_*).
Teléfonos: variables ALLOWLIST_* y ALERTA_WA_* en .env (semilla en BD_ESTRATEGIA).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Iterable

from dotenv import load_dotenv

load_dotenv(override=True)

ROLE_PRIORITY: tuple[str, ...] = (
    "ADMIN",
    "SOCIO",
    "ADMIN_COMPRAS",
    "JEFE_BARRA",
    "JEFE_COCINA",
    "STAFF_BARRA",
    "STAFF_COCINA",
    "OPS_ALERTAS",
)

# Rol → variable .env con lista de teléfonos (chat)
ROLE_ALLOWLIST_ENV: dict[str, str] = {
    "SOCIO": "ALLOWLIST_SOCIO",
    "ADMIN_COMPRAS": "ALLOWLIST_ADMIN_COMPRAS",
    "JEFE_BARRA": "ALLOWLIST_JEFE_BARRA",
    "JEFE_COCINA": "ALLOWLIST_JEFE_COCINA",
    "STAFF_BARRA": "ALLOWLIST_STAFF_BARRA",
    "STAFF_COCINA": "ALLOWLIST_STAFF_COCINA",
}

# Rol → variable .env WA individual (alertas)
ROLE_WA_ENV: dict[str, str] = {
    "ADMIN": "ALERTA_WA_FELIPE",
    "ADMIN_COMPRAS": "ALERTA_WA_MARY",
    "JEFE_BARRA": "ALERTA_WA_EDUARDO",
    "JEFE_COCINA": "ALERTA_WA_JACKY",
}

OPS_WA_ENVS: tuple[str, ...] = (
    "ALERTA_WA_MOISES",
    "ALERTA_WA_ISRAEL",
)

TOOLS_ESCRITURA = frozenset({"trasladar_mp", "conteo_iniciar", "produccion_subreceta"})

COMANDOS_OPERATIVO = (
    "APROBAR TODO",
    "APROBAR",
    "RECHAZAR",
    "KARDEX",
    "CSV",
    "INICIAR CONTEO",
    "PRODUCIR SUB",
    "PREPARAR SUB",
    "PRODUCCION SUB",
)


def _norm_tel(telefono: str) -> str:
    return (telefono or "").strip().lstrip("+")


def _phones_from_env(key: str) -> set[str]:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return set()
    return {_norm_tel(p) for p in re.split(r"[,;\s\n]+", raw) if p.strip()}


@lru_cache(maxsize=1)
def _phone_to_roles() -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}

    def add(tel: str, rol: str) -> None:
        t = _norm_tel(tel)
        if not t:
            return
        mapping.setdefault(t, set()).add(rol)

    for rol, env_key in ROLE_ALLOWLIST_ENV.items():
        for t in _phones_from_env(env_key):
            add(t, rol)

    for rol, env_key in ROLE_WA_ENV.items():
        for t in _phones_from_env(env_key):
            add(t, rol)

    for env_key in OPS_WA_ENVS:
        for t in _phones_from_env(env_key):
            add(t, "OPS_ALERTAS")

    # Legacy fallback (migración gradual)
    for t in _phones_from_env("ALLOWLIST_CONSULTA"):
        add(t, "SOCIO")
    for t in _phones_from_env("ALLOWLIST_OPERATIVO"):
        add(t, "STAFF_BARRA")

    return mapping


def phone_roles(telefono: str) -> set[str]:
    return set(_phone_to_roles().get(_norm_tel(telefono), set()))


def primary_role(telefono: str) -> str | None:
    roles = phone_roles(telefono)
    if not roles:
        return None
    for r in ROLE_PRIORITY:
        if r in roles:
            return r
    return next(iter(roles))


def get_rol(telefono: str) -> str | None:
    """Compat whatsapp_webhook: rol principal o None."""
    return primary_role(telefono)


def _cfg_tokens(key: str, default: str = "") -> set[str]:
    from config_sheets import cfg, cfg_tokens

    v = cfg(key, None)
    if v is None:
        return cfg_tokens(key, set(default.replace(" ", "").split(",")) if default else set())
    if isinstance(v, str):
        return cfg_tokens(key, set())
    return {str(x).strip().upper() for x in v if str(x).strip()}


def _cfg_bool(key: str, default: bool = False) -> bool:
    from config_sheets import cfg

    v = cfg(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "si", "sí")


def roles_con_permiso(clave_csv: str) -> set[str]:
    return _cfg_tokens(clave_csv)


def puede_ver_costos(telefono: str) -> bool:
    roles = phone_roles(telefono)
    allowed = roles_con_permiso("perm_ver_costos_roles")
    return bool(roles & allowed)


def tools_requieren_costos() -> set[str]:
    return _cfg_tokens(
        "perm_ver_costos_tools",
        "costo_plato,receta_ingredientes,costo_subreceta,inventario_valorizado,"
        "inventario_por_bodega,compras_facturas_rango,compras_factura_detalle,"
        "auditar_costos_recetas,resumen_operativo_hoy",
    )


def tools_receta_sin_costos() -> set[str]:
    return frozenset({"costo_plato", "receta_ingredientes", "costo_subreceta"})


def autorizado_tool(telefono: str, tool_name: str) -> bool:
    roles = phone_roles(telefono)
    if not roles:
        return False

    if "ADMIN" in roles:
        return True

    if tool_name in tools_requieren_costos():
        if tool_name in tools_receta_sin_costos():
            if puede_ver_costos(telefono):
                return True
            return bool(roles & roles_con_permiso("perm_receta_sin_costos_roles"))
        return puede_ver_costos(telefono)

    inv_tools = _cfg_tokens(
        "perm_inventario_consulta_tools",
        "stock_critico,stocks_negativos,stock_ingrediente,bodega_producto,mp_incompletas,kardex",
    )
    if tool_name in inv_tools:
        return bool(roles & roles_con_permiso("perm_inventario_consulta_roles"))

    ventas_ok = roles_con_permiso(
        "perm_ventas_consulta_roles",
        "ADMIN,SOCIO,ADMIN_COMPRAS,JEFE_BARRA,JEFE_COCINA,STAFF_BARRA,STAFF_COCINA",
    )
    if tool_name.startswith("ventas") or tool_name in (
        "compras_facturas_rango",
        "compras_factura_detalle",
        "consumo_ingrediente_recetas",
        "ventas_por_plato",
        "ventas_dia",
        "ventas_por_dia",
        "rotacion_baja",
        "facturas_parciales",
        "items_pendientes_factura",
    ):
        if tool_name in tools_requieren_costos():
            return puede_ver_costos(telefono)
        return bool(roles & ventas_ok)

    if tool_name in TOOLS_ESCRITURA:
        if tool_name == "trasladar_mp":
            return bool(roles & roles_con_permiso("perm_traslado_roles"))
        if tool_name == "conteo_iniciar":
            return bool(roles & roles_con_permiso("perm_conteo_iniciar_roles"))
        if tool_name == "produccion_subreceta":
            return bool(roles & roles_con_permiso("perm_producir_sub_roles"))

    if tool_name.startswith("conteo_"):
        return bool(
            roles
            & (
                roles_con_permiso("perm_conteo_iniciar_roles")
                | roles_con_permiso("perm_conteo_aprobar_roles")
            )
        )

    if "SOCIO" in roles and not _cfg_bool("perm_socio_escritura", False):
        return tool_name not in TOOLS_ESCRITURA

    # Staff / jefes: lectura general permitida si no es tool de costos bloqueado
    if roles & {"JEFE_BARRA", "JEFE_COCINA", "STAFF_BARRA", "STAFF_COCINA", "ADMIN_COMPRAS"}:
        return True

    return "SOCIO" in roles


def autorizado_comando(telefono: str, comando: str) -> bool:
    roles = phone_roles(telefono)
    if not roles:
        return False
    if "ADMIN" in roles:
        return True
    cmd = (comando or "").upper()
    if cmd.startswith("APROBAR") or cmd.startswith("RECHAZAR"):
        return bool(roles & roles_con_permiso("perm_conteo_aprobar_roles"))
    if any(cmd.startswith(c) for c in COMANDOS_OPERATIVO):
        return bool(
            roles
            & (
                roles_con_permiso("perm_conteo_iniciar_roles")
                | roles_con_permiso("perm_producir_sub_roles")
                | roles_con_permiso("perm_conteo_aprobar_roles")
                | {"SOCIO"}
            )
        )
    return "SOCIO" in roles


def _autorizado_produccion_sub(telefono: str) -> bool:
    return bool(phone_roles(telefono) & roles_con_permiso("perm_producir_sub_roles"))


_BODEGA_DEFAULT_PROD_SUB: dict[str, str] = {
    "STAFF_COCINA": "BOD-001",
    "JEFE_COCINA": "BOD-001",
    "STAFF_BARRA": "BOD-002",
    "JEFE_BARRA": "BOD-002",
}

_BODEGAS_PROD_SUB_FALLBACK: dict[str, set[str]] = {
    "STAFF_COCINA": {"BOD-001"},
    "JEFE_COCINA": {"BOD-001", "BOD-005"},
    "STAFF_BARRA": {"BOD-002"},
    "JEFE_BARRA": {"BOD-002"},
}


def bodega_default_produccion_sub(telefono: str) -> str:
    """Bodega por defecto al producir subrecetas según rol del usuario."""
    rol = primary_role(telefono)
    if rol and rol in _BODEGA_DEFAULT_PROD_SUB:
        return _BODEGA_DEFAULT_PROD_SUB[rol]
    roles = phone_roles(telefono)
    if roles & {"STAFF_COCINA", "JEFE_COCINA"}:
        return "BOD-001"
    return "BOD-002"


def bodegas_permitidas_produccion_sub(telefono: str) -> set[str]:
    """Bodegas donde el usuario puede registrar producción de subrecetas."""
    roles = phone_roles(telefono)
    if "ADMIN" in roles:
        return {"BOD-001", "BOD-002", "BOD-005"}
    allowed: set[str] = set()
    for rol in roles:
        key = f"perm_producir_sub_bodegas_{rol}"
        bods = _cfg_tokens(key)
        if not bods and rol in _BODEGAS_PROD_SUB_FALLBACK:
            bods = set(_BODEGAS_PROD_SUB_FALLBACK[rol])
        allowed |= bods
    if not allowed:
        allowed.add(bodega_default_produccion_sub(telefono))
    return allowed


def validar_bodega_produccion_sub(telefono: str, bodega: str) -> str | None:
    """None si OK; mensaje de error si la bodega no está permitida para el rol."""
    bod = (bodega or "").strip().upper()
    if not bod.startswith("BOD-"):
        return f"Bodega inválida: '{bodega}'"
    permitidas = bodegas_permitidas_produccion_sub(telefono)
    if bod in permitidas:
        return None
    return (
        f"No puedes producir en {bod}. "
        f"Bodegas permitidas: {', '.join(sorted(permitidas))}."
    )


def periodo_pruebas_cocina_activo() -> bool:
    """Pruebas operativas: cocina puede operar aunque el stock en Sheets sea insuficiente."""
    raw = (os.getenv("TATAMI_PERIODO_PRUEBAS_COCINA") or "1").strip().lower()
    return raw in ("1", "true", "yes", "si", "sí")


def es_personal_cocina(telefono: str) -> bool:
    roles = phone_roles(telefono)
    return bool(roles & {"JEFE_COCINA", "STAFF_COCINA"})


def periodo_pruebas_ignorar_stock(telefono: str) -> bool:
    """Jacky y staff cocina: no bloquear por stock insuficiente durante pruebas."""
    return periodo_pruebas_cocina_activo() and es_personal_cocina(telefono)


def telefonos_por_roles(role_codes: Iterable[str]) -> list[tuple[str, str]]:
    """
    Resuelve códigos de rol → [(tel, etiqueta)] sin duplicados.
    OPS_ALERTAS usa ALERTA_WA_MOISES / ISRAEL.
    """
    wanted = {str(r).strip().upper() for r in role_codes if str(r).strip()}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(tel: str, label: str) -> None:
        t = _norm_tel(tel)
        if not t or t in seen:
            return
        seen.add(t)
        out.append((t, label))

    for rol in ROLE_PRIORITY:
        if rol not in wanted:
            continue
        if rol == "OPS_ALERTAS":
            for env_key in OPS_WA_ENVS:
                for t in _phones_from_env(env_key):
                    push(t, env_key.replace("ALERTA_WA_", ""))
            continue
        env_wa = ROLE_WA_ENV.get(rol)
        if env_wa:
            for t in _phones_from_env(env_wa):
                push(t, rol)
        env_list = ROLE_ALLOWLIST_ENV.get(rol)
        if env_list:
            for t in _phones_from_env(env_list):
                push(t, rol)

    return out


def alertas_wa_cocina_activas() -> bool:
    """False = ningún WA de digest/alertas cocina (solo barra por ahora)."""
    from config_sheets import cfg

    v = cfg("alert_cocina_wa_activo")
    if v is not None:
        return bool(v)
    return bool(cfg("area_cocina_inventario_gestionado", False))


def alertas_wa_barra_activas() -> bool:
    from config_sheets import cfg

    return bool(cfg("area_barra_inventario_gestionado", True))


def area_bodegas_barra() -> set[str]:
    from config_sheets import cfg_tokens

    return cfg_tokens("area_barra_bodegas", {"BOD-002", "BOD-003"})


def area_bodegas_cocina() -> set[str]:
    from config_sheets import cfg_tokens

    return cfg_tokens("area_cocina_bodegas", {"BOD-001", "BOD-005"})


def filtrar_items_bodega_barra(items: list[dict]) -> list[dict]:
    bod = area_bodegas_barra()
    return [
        it
        for it in items
        if (it.get("cod_bodega") or "").strip().upper() in bod
    ]


def telefonos_alerta(clave_roles_csv: str) -> list[tuple[str, str]]:
    """Lee clave BD_CONFIG con CSV de roles (ej. alert_stock_negativo_roles_barra)."""
    clave_l = (clave_roles_csv or "").lower()
    if "cocina" in clave_l and not alertas_wa_cocina_activas():
        return []
    if "barra" in clave_l and not alertas_wa_barra_activas():
        return []
    roles = roles_con_permiso(clave_roles_csv)
    return telefonos_por_roles(roles)


def sched_horario_activo() -> bool:
    from config_sheets import cfg

    modo = str(cfg("sched_modo", "horario_secuencial") or "").strip().lower()
    return modo == "horario_secuencial" and not _cfg_bool("sched_legacy_cuadrante_activo", False)


def horas_pipeline_sri_descarga() -> set[int]:
    from config_sheets import cfg

    modo = str(cfg("pipe_facturas_sri_modo", "solo_proceso_cola") or "").strip().lower()
    if modo in ("solo_proceso", "solo_proceso_cola", "solo-cola", "cola"):
        return set()

    raw = str(cfg("pipe_facturas_sri_horas_descarga", "") or "")
    horas: set[int] = set()
    for p in raw.replace(";", ",").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            horas.add(int(p) % 24)
        except ValueError:
            pass
    return horas or set(range(24))


def pipeline_sri_solo_proceso() -> bool:
    """True si el pipeline horario solo procesa cola DESCARGADO (sin abrir portal)."""
    env = os.getenv("PIPELINE_SRI_SOLO_PROCESO", "").strip().lower()
    if env in ("1", "true", "yes", "si"):
        return True

    from config_sheets import cfg

    modo = str(cfg("pipe_facturas_sri_modo", "solo_proceso_cola") or "").strip().lower()
    return modo in ("solo_proceso", "solo_proceso_cola", "solo-cola", "cola")


def invalidar_cache() -> None:
    _phone_to_roles.cache_clear()
    try:
        from config_sheets import cargar_bd_config

        cargar_bd_config.cache_clear()
    except Exception:
        pass

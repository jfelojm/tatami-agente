"""Copy y menú WhatsApp por rol — mensajes cortos para operación diaria."""

from __future__ import annotations

import re
import time

from estrategia_config import phone_roles, primary_role

_MENU_CTX: dict[str, float] = {}
MENU_CTX_TTL_SEC = 900


def es_personal_operativo(telefono: str) -> bool:
    """Cocina/barra sin rol admin/socio — mensajes simples."""
    roles = phone_roles(telefono)
    if roles & {"ADMIN", "SOCIO"}:
        return False
    return bool(roles & {"JEFE_COCINA", "STAFF_COCINA", "JEFE_BARRA", "STAFF_BARRA"})


def es_admin_o_socio(telefono: str) -> bool:
    return bool(phone_roles(telefono) & {"ADMIN", "SOCIO"})


def es_comando_menu(texto: str) -> bool:
    t = (texto or "").strip().lower().replace("í", "i")
    if not t:
        return False
    if t in (
        "menu",
        "menú",
        "ayuda",
        "help",
        "hola",
        "hi",
        "inicio",
        "opciones",
        "comandos",
        "?",
        "buenas",
        "buenos dias",
        "buenas tardes",
    ):
        return True
    return bool(re.match(r"^(menu|ayuda)\s*$", t, re.I))


def menu_ctx_touch(telefono: str) -> None:
    _MENU_CTX[telefono] = time.monotonic()


def menu_ctx_activo(telefono: str) -> bool:
    ts = _MENU_CTX.get(telefono)
    if not ts:
        return False
    if time.monotonic() - ts > MENU_CTX_TTL_SEC:
        _MENU_CTX.pop(telefono, None)
        return False
    return True


def parse_seleccion_menu(texto: str) -> int | None:
    t = (texto or "").strip()
    if re.match(r"^[1-5]$", t):
        return int(t)
    return None


def msg_menu_principal(telefono: str) -> str:
    rol = primary_role(telefono) or ""
    operativo = es_personal_operativo(telefono)
    lines = ["*Tatami Agente*", "", "¿Qué necesitas? Responde con el número:"]
    lines.append("1 Trasladar insumo o semi")
    lines.append("2 Producir subreceta")
    if not operativo or es_admin_o_socio(telefono):
        lines.append("3 Ventas de hoy")
    else:
        lines.append("3 Ventas de hoy")
    lines.append("4 Conteo físico")
    lines.append("5 Buscar stock de un producto")
    lines.append("")
    lines.append(
        "También puedes escribir directo, por ejemplo:\n"
        "• *2 tortas de chocolate de externa a cocina*\n"
        "• *producir pan bao cocina*\n"
        "• *ventas de hoy*"
    )
    if operativo:
        lines.append("")
        lines.append("_Tip: en traslados y producción responde SÍ o NO para confirmar._")
    if rol:
        lines.append(f"\n(Rol: {rol.replace('_', ' ').title()})")
    return "\n".join(lines)


def msg_submenu(opcion: int, telefono: str) -> str:
    if opcion == 1:
        return (
            "*Traslado entre bodegas*\n\n"
            "Escribe en una línea qué, cuánto y de dónde a dónde:\n\n"
            "• *2 tortas de chocolate de externa a cocina*\n"
            "• *10 kg papa de cocina a externa*\n"
            "• *750 ml buchanan de consignación a barra*\n\n"
            "Bodegas: *cocina*, *barra*, *externa*, *consignación* "
            "(o códigos 001, 002, 005)."
        )
    if opcion == 2:
        return (
            "*Producir subreceta*\n\n"
            "• *producir pan bao cocina*\n"
            "• *2 tortas de chocolate cocina*\n"
            "• *PRODUCIR SUB 051 BOD-002* (barra)\n\n"
            "Responde *BARRA* o *COCINA* si te pide el área."
        )
    if opcion == 3:
        return (
            "*Ventas*\n\n"
            "Escribe *ventas de hoy* o *ventas de ayer*.\n"
            "Para un mes: *ventas mayo 2026*."
        )
    if opcion == 4:
        return (
            "*Conteo físico*\n\n"
            "Iniciar: *INICIAR CONTEO BOD-001* (cocina) o *BOD-002* (barra).\n"
            "Luego sigue las instrucciones de la hoja."
        )
    if opcion == 5:
        return (
            "*Stock de un producto*\n\n"
            "Escribe el nombre, por ejemplo:\n"
            "• *stock de papa*\n"
            "• *dónde está el buchanan*"
        )
    return msg_menu_principal(telefono)


def msg_confirmacion_traslado(
    telefono: str,
    *,
    cant_txt: str,
    etiqueta: str,
    origen: str,
    destino: str,
    stock_origen: float,
    unidad_base: str,
    stock_insuficiente: bool,
    periodo_pruebas: bool,
    sin_fila_maestro: bool,
) -> str:
    lines = [
        f"¿Traslado *{cant_txt}* de *{etiqueta}*?",
        f"*{origen}* → *{destino}*",
        "",
        f"Stock en origen: {stock_origen:g} {unidad_base}",
        "",
        "Responde *SÍ* para ejecutar o *NO* para cancelar.",
        "_Puedes ajustar antes (ej. *2* o *3 tortas*)._",
    ]
    avisos: list[str] = []
    if periodo_pruebas and stock_insuficiente:
        if es_personal_operativo(telefono):
            avisos.append("⚠ En origen hay 0 — se registrará igual (modo prueba).")
        else:
            avisos.append(
                f"⚠ Stock insuficiente ({stock_origen:g} {unidad_base}) — periodo pruebas."
            )
    if sin_fila_maestro:
        if es_admin_o_socio(telefono):
            avisos.append(
                "⚠ Semi sin fila en inventario — simulación. "
                "Sincroniza maestro antes de operación real."
            )
        else:
            avisos.append("⚠ Aún no está en inventario — solo simulación por ahora.")
    if avisos:
        lines.append("")
        lines.extend(avisos)
    return "\n".join(lines)


def msg_traslado_ejecutado(
    telefono: str,
    *,
    cant_txt: str,
    etiqueta: str,
    origen: str,
    destino: str,
) -> str:
    return (
        f"✓ *Traslado hecho*\n"
        f"{cant_txt} de *{etiqueta}*\n"
        f"{origen} → {destino}\n\n"
        "Escribe *menú* para más opciones."
    )


def msg_traslado_cancelado() -> str:
    return "Traslado cancelado. Escribe *menú* si necesitas otra cosa."


def msg_produccion_pie_confirmacion() -> str:
    return "\n\nResponde *SÍ* o *confirmo* para registrar en inventario."

"""Copy y menú WhatsApp por rol — mensajes cortos para operación diaria."""

from __future__ import annotations

import re
import time

from estrategia_config import (
    phone_roles,
    primary_role,
    puede_consultar_inventario,
    puede_consultar_ventas,
)

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


def opciones_menu(telefono: str) -> list[tuple[int, str, int]]:
    """(número mostrado, etiqueta, id submenú legacy 1-5)."""
    opts: list[tuple[int, str, int]] = [
        (1, "Trasladar insumo o semi", 1),
        (2, "Producir subreceta", 2),
    ]
    n = 3
    if puede_consultar_ventas(telefono):
        opts.append((n, "Ventas de hoy", 3))
        n += 1
    opts.append((n, "Conteo físico", 4))
    n += 1
    if puede_consultar_inventario(telefono):
        opts.append((n, "Buscar stock de un producto", 5))
    return opts


def resolve_menu_seleccion(telefono: str, texto: str) -> int | None:
    """Traduce el número elegido al id de submenú (1-5)."""
    sel = parse_seleccion_menu(texto)
    if sel is None:
        return None
    for num, _, sub_id in opciones_menu(telefono):
        if num == sel:
            return sub_id
    return None


def msg_menu_principal(telefono: str) -> str:
    rol = primary_role(telefono) or ""
    operativo = es_personal_operativo(telefono)
    lines = ["*Tatami Agente*", "", "¿Qué necesitas? Responde con el número:"]
    for num, label, _ in opciones_menu(telefono):
        lines.append(f"{num} {label}")
    lines.append("")
    lines.append(
        "También puedes escribir directo, por ejemplo:\n"
        "• *2 tortas de chocolate de externa a cocina*\n"
        "• *producir pan bao cocina*"
    )
    if puede_consultar_ventas(telefono):
        lines.append("• *ventas de hoy*")
    if puede_consultar_inventario(telefono):
        lines.append("• *stock de papa*")
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
            "Iniciar: *INICIAR CONTEO BOD-001* (cocina), *BOD-005* (externa) o *BOD-002* (barra).\n"
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
    permitir_stock_negativo: bool,
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
    if permitir_stock_negativo and stock_insuficiente:
        avisos.append(
            f"⚠ Stock insuficiente en origen ({stock_origen:g} {unidad_base}). "
            "Si confirmas, se registra igual y se envía aviso a administración."
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


def _normalizar_respuesta_corta(texto: str) -> str:
    t = (texto or "").strip().lower()
    for old, new in (("í", "i"), ("á", "a"), ("é", "e"), ("ó", "o"), ("ú", "u")):
        t = t.replace(old, new)
    return t


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def es_confirmacion_corta(texto: str) -> bool:
    """SÍ, confirmo, y typos frecuentes en móvil (ej. «ai» por «si»)."""
    t = _normalizar_respuesta_corta(texto)
    if not t:
        return False
    if t in (
        "si",
        "si confirmo",
        "confirmo",
        "confirmar",
        "ok",
        "dale",
        "aplicar",
        "yes",
        "listo",
        "de acuerdo",
        "claro",
        "si confirmo el traslado",
        "confirmo el traslado",
        "ai",
        "sii",
        "sip",
        "sep",
        "sim",
    ):
        return True
    if t.startswith("si ") and "confirm" in t:
        return True
    if 2 <= len(t) <= 3 and t.isalpha() and _edit_distance(t, "si") <= 1:
        return True
    return False


def es_cancelacion_corta(texto: str) -> bool:
    t = _normalizar_respuesta_corta(texto)
    if t in ("no cancelar", "no canceles", "sin cancelar", "no cancela"):
        return False
    return t in (
        "no",
        "cancelar",
        "cancela",
        "cancel",
        "olvida",
        "olvidalo",
        "no confirmo",
        "detener",
        "stop",
        "nop",
    )


def msg_recordatorio_confirmacion_traslado() -> str:
    return (
        "Tienes un *traslado pendiente* de confirmar.\n\n"
        "Responde *SÍ* para ejecutarlo o *NO* para cancelar.\n"
        "También puedes ajustar la cantidad (ej. *3 tortas*)."
    )


def msg_recordatorio_confirmacion_produccion() -> str:
    return (
        "Tienes una *producción pendiente* de confirmar.\n\n"
        "Responde *SÍ* o *confirmo* para registrar, o *NO* para cancelar."
    )


def parece_nueva_operacion(texto: str) -> bool:
    """True si el mensaje no debería bloquearse ante un pending de confirmación."""
    t = (texto or "").strip()
    if len(t) > 45:
        return True
    tl = _normalizar_respuesta_corta(t)
    if es_comando_menu(t):
        return True
    if re.search(r"\b(ventas?|traslad|transfer|produc|prepar|conteo|stock|inventar)\w*", tl):
        return True
    if re.search(r"\bde\b.*\ba\b", tl) and re.search(r"\b0?\d{3}\b|cocina|barra|externa", tl):
        return True
    return False

# whatsapp_webhook.py v4 — sin dependencia de consultas_chat_extendidas ni agente_chat
import asyncio
import contextvars
import hashlib
import hmac
import os
import json
import math
import re
import time
import unicodedata
from collections import OrderedDict, defaultdict, deque
from datetime import date, timedelta, datetime
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client
import gspread
import anthropic
import pytz
import httpx
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from sesiones_factura import hay_sesion_activa
from ventas_smartmenu import estado_documento_excluye_neto_operativo
from factura_confirmacion_parse import parse_confirmacion_factura
from whatsapp_factura_handler import handle_confirmacion, handle_mensaje_media

from sesiones_conteo import get_sesion_activa, aprobar_items, rechazar_items, cerrar_sesion
from conteo_fisico import contabilizar_envio, ConteoOperacionError
from subreceta_operaciones import producir_subreceta_wa, SubrecetaOperacionError
from conteo_operaciones import (
    iniciar_conteo_wa,
    resumen_ciclos_abiertos,
    semana_iso_actual,
)
from kardex_inventario import get_kardex, formatear_kardex_wa, generar_xlsx
from google_credentials import google_credentials

load_dotenv()
TZ = pytz.timezone("America/Guayaquil")
LOG_DIR = Path(__file__).resolve().parent / "logs"

# Verificar en producción: GET / debe mostrar este valor tras cada deploy.
TATAMI_WA_BUILD = "20250619-railway-v17"


def _log_webhook_event(line: str) -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "webhook_inbound.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
    except Exception as e:
        print(f"[Meta webhook] log error: {e}")

def _norm_tel(telefono: str) -> str:
    return (telefono or "").strip().lstrip("+")


from estrategia_config import (
    autorizado_comando as _estrategia_autorizado_comando,
    autorizado_tool as _estrategia_autorizado_tool,
    get_rol,
    phone_roles,
    puede_ver_costos,
)

TOOLS_ESCRITURA_SOCIO_OPERATIVO = {
    "trasladar_mp",
    "conteo_iniciar",
    "produccion_subreceta",
}

COMANDOS_OPERATIVO = {
    "APROBAR TODO", "APROBAR", "RECHAZAR", "KARDEX", "CSV", "INICIAR CONTEO",
    "PRODUCIR SUB", "PREPARAR SUB", "PRODUCCION SUB",
}

MSG_NO_AUTORIZADO = "No tienes acceso a este agente."
MSG_ERROR_PROCESO = (
    "Hubo un error procesando tu mensaje. Intenta de nuevo en un momento."
)
MSG_AYUDA_SUBRECETA = (
    "Subrecetas — ¿qué necesitas?\n"
    "• *Catálogo:* lista subrecetas\n"
    "• *Producir cocina:* PRODUCIR SUB 006 BOD-001 (pan bao)\n"
    "• *Producir barra:* PRODUCIR SUB 051 BOD-002\n"
    "• *Costo:* costo de salsa ponzu\n"
    "Responde *BARRA* o *COCINA* para elegir área de producción."
)
MSG_TIPO_NO_SOPORTADO = (
    "Solo procesamos mensajes de texto (y fotos/PDF de facturas).\n"
    "Para producir una subreceta escribe por ejemplo:\n"
    "PRODUCIR SUB 006 BOD-001 (cocina) o PRODUCIR SUB 051 BOD-002 (barra)\n"
    "(simula) o añade CONFIRMAR al final para aplicar."
)
MSG_PROCESANDO = "Recibí tu mensaje, dame un momento..."

def _msg_menu_produccion_area(area: str) -> str:
    if area == "cocina":
        return (
            "Producción *cocina* (BOD-001). Dime el nombre o código:\n"
            "• pan bao (006) | salsa ponzu (016) | kimchi (036)\n"
            "• mayonesa ponzu (017) | salsa gochuyan (009) | char siu (055)\n"
            "Ejemplo: PRODUCIR SUB 006 BOD-001\n"
            "o: preparar pan bao"
        )
    return (
        "Producción *barra* (BOD-002). Dime el nombre o código:\n"
        "• 051 Negroni | 052 Tokio Mule | 053 Ron Banana | 054 Mojito coco\n"
        "Ejemplo: PRODUCIR SUB 053 BOD-002\n"
        "o: preparar 1100 ml batch ron banana negroni"
    )


def _msg_batch_preguntar_area() -> str:
    return (
        "¿La subreceta es de *barra* o de *cocina*?\n"
        "Responde: BARRA o COCINA\n\n"
        "• Barra (BOD-002): batches 051–054\n"
        "• Cocina (BOD-001): salsas, pan bao, kimchi… (002–050, 055–059)"
    )


def _msg_batch_no_identificado(wa_id: str, area: str | None = None) -> str:
    from estrategia_config import phone_roles, primary_role

    if area in ("barra", "cocina"):
        return _msg_menu_produccion_area(area)

    rol = primary_role(wa_id) or ""
    roles = phone_roles(wa_id)
    solo_cocina = roles <= {"STAFF_COCINA", "JEFE_COCINA"} and bool(roles)
    solo_barra = roles <= {"STAFF_BARRA", "JEFE_BARRA"} and bool(roles)
    if rol in ("STAFF_COCINA", "JEFE_COCINA") and (solo_cocina or not roles & {"STAFF_BARRA", "JEFE_BARRA"}):
        return _msg_menu_produccion_area("cocina")
    if rol in ("STAFF_BARRA", "JEFE_BARRA") and (solo_barra or not roles & {"STAFF_COCINA", "JEFE_COCINA"}):
        return _msg_menu_produccion_area("barra")
    return _msg_batch_preguntar_area()


def _parse_area_produccion(texto: str) -> str | None:
    t = (texto or "").strip().lower()
    if not t:
        return None
    if t in ("barra", "de barra", "en barra", "1", "b"):
        return "barra"
    if t in ("cocina", "de cocina", "en cocina", "2", "c"):
        return "cocina"
    if re.search(r"\bbarra\b", t) and not re.search(r"\bcocina\b", t):
        return "barra"
    if re.search(r"\bcocina\b", t) and not re.search(r"\bbarra\b", t):
        return "cocina"
    return None


def _bodega_por_area(area: str | None) -> str | None:
    if area == "barra":
        return "BOD-002"
    if area == "cocina":
        return "BOD-001"
    return None


def _prod_ctx_get(wa_id: str | None) -> dict:
    if not wa_id:
        return {}
    ctx = _pending_prod_ctx.get(wa_id)
    if not ctx:
        return {}
    if time.monotonic() - ctx.get("at", 0) > _PROD_CTX_TTL_SEC:
        _pending_prod_ctx.pop(wa_id, None)
        return {}
    return ctx


def _prod_ctx_touch(wa_id: str, **updates) -> None:
    ctx = _prod_ctx_get(wa_id) or {}
    ctx.update(updates)
    ctx["at"] = time.monotonic()
    _pending_prod_ctx[wa_id] = ctx
    area = updates.get("area") or ctx.get("area")
    if area in ("barra", "cocina"):
        _pending_prod_area[wa_id] = area


def _inferir_area_desde_cods(cods: list[str]) -> str | None:
    if not cods:
        return None
    from codigos_subreceta import cod_sub_canonico
    from subrecetas_bodegas_stock import SUBRECETAS_BARRA

    areas: set[str] = set()
    for c in cods:
        if cod_sub_canonico(c) in SUBRECETAS_BARRA:
            areas.add("barra")
        else:
            areas.add("cocina")
    return areas.pop() if len(areas) == 1 else None


def _resolver_area_produccion(
    wa_id: str | None,
    texto: str,
    cods: list[str] | None = None,
    area_hint: str | None = None,
) -> str | None:
    area = area_hint or _parse_area_produccion(texto)
    if not area and cods:
        area = _inferir_area_desde_cods(cods)
    if not area and wa_id:
        area = _prod_ctx_get(wa_id).get("area")
    if not area and wa_id:
        pa = _pending_prod_area.get(wa_id)
        if pa in ("barra", "cocina"):
            area = pa
    return area


def _msg_pedir_nombre_sub(area: str) -> str:
    bod = _bodega_por_area(area) or "BOD-001"
    return (
        f"Producción *{area}* ({bod}). Dime nombre o código y cantidad.\n"
        f"Ejemplo: preparar aceite jengibre 200gr\n"
        f"o: PRODUCIR SUB 044 {bod}"
    )


def _conteo_ctx_get(wa_id: str | None) -> dict:
    if not wa_id:
        return {}
    ctx = _pending_conteo_ctx.get(wa_id)
    if not ctx:
        return {}
    if time.monotonic() - ctx.get("at", 0) > _CONTEO_CTX_TTL_SEC:
        _pending_conteo_ctx.pop(wa_id, None)
        return {}
    return ctx


def _conteo_ctx_touch(wa_id: str, **updates) -> None:
    ctx = _conteo_ctx_get(wa_id) or {}
    ctx.update(updates)
    ctx["at"] = time.monotonic()
    _pending_conteo_ctx[wa_id] = ctx


def _limpiar_ctx_conteo(wa_id: str) -> None:
    _pending_conteo_ctx.pop(wa_id, None)


def _limpiar_pick_produccion(wa_id: str) -> None:
    if _pending_prod_area.get(wa_id) == "pick":
        _pending_prod_area.pop(wa_id, None)


def _ultima_linea_usuario(texto: str) -> str:
    lines = [ln.strip() for ln in (texto or "").splitlines() if ln.strip()]
    return lines[-1] if lines else (texto or "").strip()


def _parse_bodega_conteo(texto: str) -> str | None:
    m = re.search(r"\b(bod-\d{3})\b", (texto or ""), re.I)
    if m:
        return m.group(1).upper()
    area = _parse_area_produccion(texto)
    return _bodega_por_area(area)


def _es_mensaje_conteo(texto: str, wa_id: str | None = None) -> bool:
    raw = (texto or "").strip()
    if not raw:
        return False
    t = raw.lower()
    ultima = _ultima_linea_usuario(raw).lower()
    if re.search(r"\bconteo", t):
        return True
    if re.search(r"inventario\s+f[ií]sico", t):
        return True
    if re.search(r"\b(iniciar|nuevo|empezar|crear)\s+conteo\b", t):
        return True
    if "conteo_barra" in t or "borrador_conteo" in t or "ciclo de conteo" in t:
        return True
    ctx = _conteo_ctx_get(wa_id) if wa_id else {}
    if not ctx.get("active"):
        return False
    for fragment in (ultima, t):
        if len(fragment) <= 48 and _parse_bodega_conteo(fragment):
            return True
    if re.search(r"\b(revisar|iniciar|nuevo|enviar|aprobar)\b", ultima):
        return True
    return False


def _texto_resumen_conteo_wa(ciclos: list[dict], *, bod: str | None = None) -> str:
    if bod:
        ciclos = [c for c in ciclos if (c.get("cod_bodega") or "").upper() == bod.upper()]
    lines: list[str] = []
    if not ciclos:
        if bod:
            lines.append(f"No hay ciclos de conteo abiertos en {bod}.")
        else:
            lines.append("No hay ciclos de conteo abiertos.")
    else:
        lines.append(f"Ciclos de conteo abiertos ({len(ciclos)}):\n")
        for c in ciclos:
            lines.append(
                f"*{c.get('sheet_name') or '?'}* ({c.get('cod_bodega')})\n"
                f"- Estado: {c.get('estado')}\n"
                f"- Semana: {c.get('semana_iso')} de {c.get('anio')}\n"
                f"- ID: {c.get('ciclo_id')}\n"
            )
    lines.append(
        "Qué puedes hacer:\n"
        "• INICIAR CONTEO BOD-001 (cocina) o INICIAR CONTEO BOD-002 (barra)\n"
        "• Captura en Sheets → menú Conteo → Enviar a Tatami\n"
        "• Tras envío: APROBAR TODO"
    )
    return "\n".join(lines)


async def _manejar_mensaje_conteo(wa_id: str, texto: str) -> None:
    if not (
        autorizado_tool(wa_id, "conteo_ciclos_abiertos")
        or autorizado_tool(wa_id, "conteo_iniciar")
    ):
        await enviar_mensaje_meta(wa_id, MSG_NO_AUTORIZADO)
        return
    _limpiar_pick_produccion(wa_id)
    _conteo_ctx_touch(wa_id, active=True)
    t = texto.lower()
    ultima = _ultima_linea_usuario(texto)
    bod = _parse_bodega_conteo(texto) or _parse_bodega_conteo(ultima)
    quiere_iniciar = bool(re.search(r"\b(iniciar|nuevo|empezar|crear)\b", t))
    if quiere_iniciar and bod:
        if not autorizado_tool(wa_id, "conteo_iniciar"):
            await enviar_mensaje_meta(wa_id, MSG_NO_AUTORIZADO)
            return
        try:
            r = await asyncio.to_thread(iniciar_conteo_wa, bod, sobreescribir_hoja=True)
            out = (
                f"Conteo iniciado — {r['cod_bodega']} W{r['semana_iso']}/{r['anio']}\n"
                f"ciclo_id: {r['ciclo_id']}\n"
                f"Hoja: {r['sheet_name']} ({r['mps_en_snapshot']} MPs)\n"
                f"URL: {r.get('url_hoja', '')}\n\n"
                "Pasos:\n" + "\n".join(f"• {x}" for x in r.get("instrucciones", []))
            )
        except ConteoOperacionError as e:
            out = f"No se pudo iniciar conteo: {e.message}"
        except Exception as e:
            out = f"Error: {e}"
        await enviar_mensaje_meta(wa_id, out)
        return
    r = await asyncio.to_thread(tool_conteo_ciclos_abiertos, {})
    ciclos = r.get("ciclos") or []
    await enviar_mensaje_meta(wa_id, _texto_resumen_conteo_wa(ciclos, bod=bod))


def _tokens_sub_nombre(s: str) -> list[str]:
    t = _normaliza_busqueda_mp(s)
    t = re.sub(r"[^\w\s]", " ", t)
    return [w for w in t.split() if len(w) >= 2 and w not in _SUB_STOPWORDS]


def _texto_sin_cantidad_sub(texto: str) -> str:
    sin = re.sub(
        r"\d[\d.,]*\s*(?:ml|mililitros?|gr|gramos?|g\b|uni|unidades?)",
        " ",
        texto or "",
        flags=re.I,
    )
    return re.sub(r"\s+", " ", sin).strip()


def _coincide_nombre_sub(nombre: str, texto: str) -> bool:
    nt = _tokens_sub_nombre(nombre)
    tt = set(_tokens_sub_nombre(_texto_sin_cantidad_sub(texto)))
    if not nt or not tt:
        return False
    if all(tok in tt for tok in nt):
        return True
    nf = "".join(nt)
    tf = "".join(_tokens_sub_nombre(_texto_sin_cantidad_sub(texto)))
    return len(nf) >= 4 and nf in tf


def _extraer_cantidad_sub(texto: str, cod_sub: str | None = None) -> float | None:
    """Cantidad en unidad base (gr/ml) o None → lote estándar en producción."""
    from unidades_operativas import (
        parse_cantidad_explicita_base,
        resolver_cantidad_produccion_sub,
    )

    expl = parse_cantidad_explicita_base(texto)
    if expl is not None:
        return expl
    if cod_sub:
        r = resolver_cantidad_produccion_sub(cod_sub, None, texto=texto)
        if r.get("cantidad_base") is not None:
            return float(r["cantidad_base"])
    t = (texto or "").lower()
    mc2 = re.search(r"\b(\d{4,5})\b", t)
    if mc2:
        return float(mc2.group(1).replace(",", "."))
    return None


def _es_consulta_receta_plato(texto: str) -> bool:
    """Ingredientes/receta de plato vendido (BD_RECETAS), no producción de semi."""
    if _es_mensaje_traslado(texto) or _es_consulta_lista_subrecetas(texto):
        return False
    t = (texto or "").lower()
    if re.search(r"\b(producir|produccion|producción|preparar|registrar|hacer|batch)\w*", t):
        return False
    if re.search(r"\b(subreceta|semi)\b", t) and not re.search(
        r"\b(plato|fuerte|carta|menu|venta)\b", t
    ):
        return False
    if re.search(r"\bplato\s+fuerte\b", t) and re.search(r"\b(receta|ingredientes?)\b", t):
        return True
    if re.search(r"\b(receta|ingredientes?|gramajes?|composición|composicion)\b", t):
        if re.search(r"\b(plato|fuerte|carta|menu|venta|bowl|ramen|sushi|bao|tarta)\b", t):
            return True
        if re.search(r"\b(de|del)\s+\S", t):
            return True
        if re.search(r"\b(dame|dime|lista|muéstrame|muestrame|quiero\s+saber)\b", t):
            return True
    return False


_GENERICO_NOMBRE_PLATO = frozenset({
    "un plato fuerte",
    "plato fuerte",
    "un plato",
    "plato",
    "la receta",
    "receta",
    "los ingredientes",
    "ingredientes",
    "un plato de la carta",
    "la carta",
})


def _extraer_nombre_plato_receta(texto: str) -> str:
    t = (texto or "").strip()
    if not t:
        return ""
    patterns = (
        r"(?:ingredientes?|receta|gramajes?)\s+(?:de|del)\s+(?:la?\s+)?(.+?)[\?\.]*$",
        r"(?:dame|dime|muéstrame|muestrame)\s+(?:los\s+)?(?:ingredientes?|receta)\s+(?:de|del)\s+(?:la?\s+)?(.+?)[\?\.]*$",
        r"quiero\s+saber\s+(?:la\s+)?receta\s+(?:de|del)\s+(?:la?\s+)?(.+?)[\?\.]*$",
    )
    for pat in patterns:
        m = re.search(pat, t, re.I)
        if m:
            cand = m.group(1).strip(" .,;")
            if cand.lower() not in _GENERICO_NOMBRE_PLATO:
                return cand
    return ""


def _es_pedido_nombres_mp_produccion(texto: str) -> bool:
    if _es_consulta_receta_plato(texto):
        return False
    t = (texto or "").lower()
    if re.search(r"\b(nombre|nombres|bobre|bobres)\b", t) and re.search(r"\bmp", t):
        return True
    return bool(re.search(r"\b(muestra|dame|lista|detalle)\b", t) and re.search(r"\b(mp|insumo|ingrediente)", t))


def _es_intento_produccion(texto: str, wa_id: str | None = None) -> bool:
    if _es_mensaje_traslado(texto):
        return False
    if _es_mensaje_conteo(texto, wa_id):
        return False
    if _es_consulta_lista_subrecetas(texto):
        return False
    if _es_consulta_receta_plato(texto):
        return False
    t = (texto or "").lower()
    if re.search(r"\bproducto?s?\b", t) and not re.search(
        r"\b(producir|produccion|producción|preparar|preparacion|preparación)\w*",
        t,
    ):
        return False
    if re.search(r"\b(subreceta|semi)\b", t) and not re.search(
        r"\b(producir|preparar|registrar|hacer|batch)\w*",
        t,
    ):
        return False
    return bool(
        re.search(
            r"\b(producir|produccion|producción|preparar|preparacion|preparación|registrar|hacer|batch)\w*",
            t,
        )
    )


def _es_orden_produccion_afirmativa(texto: str) -> bool:
    t = (texto or "").lower().strip()
    if re.search(r"\b(prepar|produc)\w*", t):
        return True
    return bool(t.startswith("si ") and re.search(r"\b(prepar|produc|haz|hacer)\w*", t))


def autorizado_tool(telefono: str, tool_name: str) -> bool:
    return _estrategia_autorizado_tool(telefono, tool_name)


def autorizado_comando(telefono: str, comando: str) -> bool:
    return _estrategia_autorizado_comando(telefono, comando)


def _autorizado_produccion_sub(telefono: str) -> bool:
    from estrategia_config import roles_con_permiso

    return bool(phone_roles(telefono) & roles_con_permiso("perm_producir_sub_roles"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
app = FastAPI()


@app.get("/ping")
def ping():
    return {"ok": True, "build": TATAMI_WA_BUILD}


@app.middleware("http")
async def _log_unhandled_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        import traceback

        print(f"[HTTP 500] {request.method} {request.url.path}: {e}\n{traceback.format_exc()}")
        raise


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    import traceback
    from starlette.exceptions import HTTPException as StarletteHTTPException

    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    print(f"[HTTP 500] {request.method} {request.url.path}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc),
            "type": type(exc).__name__,
            "path": str(request.url.path),
            "wa_build": TATAMI_WA_BUILD,
        },
    )

# Dedup webhook Meta (msg_id) con TTL — evita reprocesar y crece sin límite
_mensajes_procesados: OrderedDict[str, float] = OrderedDict()
MSG_DEDUP_TTL_SEC = 86400
MSG_DEDUP_MAX = 50_000

# Cola por número: un mensaje activo + pendientes (Lock + drain)
_wa_locks: dict[str, asyncio.Lock] = {}
_wa_pending: dict[str, deque] = defaultdict(deque)
_wa_runner_active: set[str] = set()
_wa_cola_avisado: set[str] = set()
# Si ya enviamos respuesta al usuario en este turno, no mandar MSG_ERROR_PROCESO
_wa_procesando_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wa_procesando_id", default=None
)
_wa_ya_respondio_turno: dict[str, bool] = {}
# Última simulación PRODUCIR SUB por wa_id (para "si confirmo" / "confirmo")
_pending_prod_sub: dict[str, dict] = {}
# Tras "producir subreceta" sin detalle: "pick" | "barra" | "cocina"
_pending_prod_area: dict[str, str] = {}
# Contexto de producción por usuario (área, última sub, catálogo visto)
_pending_prod_ctx: dict[str, dict] = {}
_PROD_CTX_TTL_SEC = 900
_SUB_STOPWORDS = frozenset({"de", "del", "la", "el", "los", "las", "un", "una", "en", "y", "con"})
# Contexto de conteo físico (no confundir barra/cocina con producción)
_pending_conteo_ctx: dict[str, dict] = {}
_CONTEO_CTX_TTL_SEC = 900
# Contexto de traslado pendiente (insumo/bodegas incompletos o aclaración sub)
_pending_traslado: dict[str, dict] = {}
_TRASLADO_CTX_TTL_SEC = 900
MSG_COLA_ESPERA = (
    "Un momento, estoy procesando tu mensaje anterior. Te respondo en seguida."
)

# Cache BD_MP_SISTEMA (reduce lecturas Sheets en ráfagas / múltiples tools)
_bd_mp_cache: list[dict] | None = None
_bd_mp_cache_at: float = 0.0
BD_MP_CACHE_TTL_SEC = 60

_bd_prov_cache: list[dict] | None = None
_bd_prov_cache_at: float = 0.0
BD_PROV_CACHE_TTL_SEC = 120

_sheet_workbook = None

from conteo_routes import router as conteo_router
app.include_router(conteo_router, prefix="/api/conteo")

from factura_manual_routes import router as factura_manual_router
app.include_router(factura_manual_router, prefix="/api/factura_manual")

from dashboard_routes import router as dashboard_router
app.include_router(dashboard_router)

# ── Helpers ──────────────────────────────────────────────────
def _to_float(v, default=0.0):
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, default)


def _to_int(v, default=0):
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _paging(args: dict | None, *, default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    """(limit, offset) estándar para listados largos."""
    args = args or {}
    limit = _to_int(args.get("limit", default_limit), default_limit)
    offset = _to_int(args.get("offset", 0), 0)
    limit = _clamp(limit, 1, max_limit)
    offset = max(0, offset)
    return limit, offset


def _normaliza_busqueda_mp(s: str) -> str:
    """Minúsculas y sin tildes para comparar nombres de MP."""
    t = (s or "").lower().strip()
    return "".join(
        c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn"
    )


def _variantes_raiz_plural(ff: str) -> set[str]:
    """Camarones/camaron, langostinos/langostino — heurística simple."""
    out = {ff}
    if len(ff) >= 4 and ff.endswith("es"):
        out.add(ff[:-2])
    if len(ff) >= 4 and ff.endswith("s") and not ff.endswith("es"):
        out.add(ff[:-1])
    return {x for x in out if len(x) >= 3}


def _coincide_nombre_mp(nombre_fila: str, filtro: str) -> bool:
    """True si el nombre en hoja coincide con lo que buscó el usuario (parcial, sin tildes)."""
    nf = _normaliza_busqueda_mp(nombre_fila)
    ff = _normaliza_busqueda_mp(filtro)
    if not ff:
        return True
    if not nf:
        return False
    for v in _variantes_raiz_plural(ff):
        if v and v in nf:
            return True
    for tok in ff.split():
        if len(tok) >= 3:
            for v in _variantes_raiz_plural(tok):
                if v and v in nf:
                    return True
    return False


# ── Conexiones ───────────────────────────────────────────────
_supabase_client = None


def conectar_supabase():
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
        )
    return _supabase_client


def conectar_sheets():
    global _sheet_workbook
    if _sheet_workbook is None:

        creds = google_credentials(SCOPES)
        _sheet_workbook = gspread.Client(auth=creds).open_by_key(
            os.getenv("SPREADSHEET_ID")
        )
    return _sheet_workbook


def _purge_mensajes_procesados() -> None:
    now = time.monotonic()
    while _mensajes_procesados:
        _mid, ts = next(iter(_mensajes_procesados.items()))
        if len(_mensajes_procesados) > MSG_DEDUP_MAX or (now - ts) > MSG_DEDUP_TTL_SEC:
            _mensajes_procesados.popitem(last=False)
        else:
            break


def mensaje_ya_procesado(msg_id: str) -> bool:
    """True si msg_id ya se encoló/procesó (dedup Meta)."""
    if not msg_id:
        return False
    _purge_mensajes_procesados()
    if msg_id in _mensajes_procesados:
        return True
    _mensajes_procesados[msg_id] = time.monotonic()
    return False


def invalidar_cache_bd_mp() -> None:
    global _bd_mp_cache, _bd_mp_cache_at
    _bd_mp_cache = None
    _bd_mp_cache_at = 0.0


def leer_bd_mp_sistema(*, force_refresh: bool = False) -> list[dict]:
    """Filas de BD_MP_SISTEMA; cache en memoria 60s salvo force_refresh."""
    global _bd_mp_cache, _bd_mp_cache_at
    now = time.monotonic()
    if (
        not force_refresh
        and _bd_mp_cache is not None
        and (now - _bd_mp_cache_at) < BD_MP_CACHE_TTL_SEC
    ):
        return list(_bd_mp_cache)

    _, rows = leer_hoja_con_headers(
        "BD_MP_SISTEMA", "cod_mp_sistema", skip_after_header=1
    )
    out = [r for r in rows if (r.get("cod_mp_sistema") or "").strip()]
    _bd_mp_cache = out
    _bd_mp_cache_at = now
    return list(out)


def leer_hoja_con_headers(sheet_name: str, header_key: str, *, skip_after_header: int = 1) -> tuple[list[str], list[dict]]:
    """
    Lee una hoja y detecta la fila de headers buscando header_key.
    Retorna (headers, rows_as_dict).
    """
    sheet = conectar_sheets()
    ws = sheet.worksheet(sheet_name)
    values = ws.get_all_values()
    header_row = None
    for i, row in enumerate(values):
        if any((c or "").strip() == header_key for c in row):
            header_row = i
            break
    if header_row is None:
        return [], []
    headers = [(c or "").strip() for c in values[header_row]]
    data = values[header_row + skip_after_header :]
    out = []
    for row in data:
        if not any((c or "").strip() for c in row):
            continue
        if row and str(row[0]).strip().startswith("["):
            continue
        d = {headers[j]: (row[j] if j < len(row) else "").strip() for j in range(len(headers)) if headers[j]}
        out.append(d)
    return headers, out


# ── Búsqueda de MP (migrada desde consultas_chat_extendidas) ─
def _tokens_busqueda_mp(texto: str) -> list[str]:
    import re

    t = re.sub(r"[''`´]", "", (texto or "").strip().lower())
    return [w for w in re.split(r"\s+", t) if w]


def _buscar_mp_por_nombre_o_codigo(texto: str) -> list[dict]:
    """
    Busca MPs en BD_MP_SISTEMA por nombre (substring) o código exacto.
    Acepta varias palabras: "buchanan 18" → nombre contiene buchanan y 18.
    Usa leer_bd_mp_sistema() con cache — no re-autentica con Sheets.
    """
    from inventario_stock_mp import norm_mp

    texto_u = (texto or "").strip().lower()
    if len(texto_u) < 2:
        return []
    tokens = _tokens_busqueda_mp(texto_u)
    rows = leer_bd_mp_sistema()
    hits: list[dict] = []
    vistos: set[tuple[str, str]] = set()

    def _add(r: dict, *, prio: int) -> None:
        cod = (r.get("cod_mp_sistema") or "").strip()
        bod = (r.get("cod_bodega") or "").strip()
        key = (norm_mp(cod), bod)
        if not cod or key in vistos:
            return
        vistos.add(key)
        hits.append({**r, "_prio": prio})

    for r in rows:
        cod = (r.get("cod_mp_sistema") or "").strip()
        nom = (r.get("nombre_mp") or "").strip()
        if not cod:
            continue
        cod_l = cod.lower()
        nom_l = nom.lower()
        if texto_u == cod_l or norm_mp(cod_l) == norm_mp(texto_u):
            _add(r, prio=0)
            continue
        if len(tokens) >= 2:
            if all(tok in nom_l or tok == cod_l for tok in tokens):
                _add(r, prio=1)
            continue
        if texto_u in nom_l:
            _add(r, prio=2)

    hits.sort(key=lambda x: (x.get("_prio", 9), x.get("nombre_mp", "")))
    for h in hits:
        h.pop("_prio", None)
    return hits


def _filas_mp_maestro(rows: list[dict], cod_mp: str) -> list[dict]:
    from inventario_stock_mp import norm_mp

    target = norm_mp(cod_mp)
    if not target:
        return []
    return [
        r
        for r in rows
        if norm_mp(r.get("cod_mp_sistema")) == target
    ]


def _limpiar_cod_mp_usuario(cod: str) -> str:
    c = (cod or "").strip()
    upper = c.upper()
    if upper.startswith("MP-"):
        return c[3:].strip()
    if upper.startswith("MP") and len(c) > 2 and c[2] in "- ":
        return c[3:].strip()
    return c


def _cods_mp_unicos(hits: list[dict]) -> list[str]:
    from inventario_stock_mp import norm_mp

    out: list[str] = []
    seen: set[str] = set()
    for h in hits:
        c = norm_mp(h.get("cod_mp_sistema"))
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return sorted(out)


def _opciones_mp_desde_hits(
    hits: list[dict],
    *,
    bodega_filtro: str = "",
) -> list[dict]:
    """Lista de MPs únicos con stock por bodega, para preguntar al usuario."""
    from bodegas_config import nombre_bodega, resolver_cod_bodega
    from inventario_stock_mp import agrupar_stock_par_por_mp

    g = agrupar_stock_par_por_mp(hits)
    bod_f = resolver_cod_bodega(bodega_filtro) if bodega_filtro else ""
    opciones: list[dict] = []
    items = sorted(
        g.items(),
        key=lambda x: (
            -(x[1]["por_bodega"].get(bod_f, 0) if bod_f else x[1]["stock_total"]),
            x[1]["nombre_mp"],
        ),
    )
    for i, (cod, info) in enumerate(items, 1):
        ub = (info.get("unidad_base") or "").strip()
        desg = ", ".join(
            f"{nombre_bodega(b)} {round(v, 2)} {ub}".strip()
            for b, v in sorted(info.get("por_bodega", {}).items())
        )
        nom = (info.get("nombre_mp") or cod).strip()
        opciones.append(
            {
                "indice": i,
                "cod_mp_sistema": cod,
                "nombre_mp": nom,
                "stock_por_bodega": info.get("por_bodega", {}),
                "stock_total": info.get("stock_total", 0),
                "unidad_base": ub,
                "texto_usuario": f"{nom} — {desg or 'sin stock'}",
            }
        )
    return opciones


def _elegir_mp_automatico(hits: list[dict], bodega_origen: str = "") -> str | None:
    """Un solo MP claro, o único con stock en bodega origen."""
    from bodegas_config import resolver_cod_bodega
    from inventario_stock_mp import agrupar_stock_par_por_mp

    g = agrupar_stock_par_por_mp(hits)
    cods = list(g.keys())
    if len(cods) == 1:
        return cods[0]
    if not bodega_origen:
        return None
    origen = resolver_cod_bodega(bodega_origen)
    if not origen:
        return None
    con_stock = [
        c
        for c in cods
        if float(g[c].get("por_bodega", {}).get(origen, 0) or 0) > 0
    ]
    if len(con_stock) == 1:
        return con_stock[0]
    return None


def _resolver_mp_por_nombre(
    rows: list[dict],
    *,
    nombre_mp: str = "",
    cod_mp: str = "",
    bodega_origen: str = "",
) -> dict:
    """
    Resuelve materia prima por nombre (prioridad) o código solo si existe en maestro.
    Nunca confía en códigos inventados.

    Retorna:
      {"ok": True, "cod_mp", "nombre_mp"}
      {"ok": False, "requiere_eleccion": True, "opciones", "mensaje"}
      {"ok": False, "error": "..."}
    """
    from inventario_stock_mp import norm_mp

    nom = (nombre_mp or "").strip()
    cod_raw = _limpiar_cod_mp_usuario(cod_mp) if cod_mp else ""
    cod_confiable = (
        norm_mp(cod_raw) if cod_raw and _filas_mp_maestro(rows, cod_raw) else ""
    )

    if cod_confiable:
        nombre = ""
        for r in _filas_mp_maestro(rows, cod_confiable):
            nombre = (r.get("nombre_mp") or "").strip()
            if nombre:
                break
        return {
            "ok": True,
            "cod_mp": cod_confiable,
            "nombre_mp": nombre or cod_confiable,
        }

    texto = nom or cod_raw
    if not texto or len(texto) < 2:
        return {
            "ok": False,
            "error": "¿Qué producto? Dime el nombre (ej. Buchanan's 18, papa, hielo).",
        }

    hits = _buscar_mp_por_nombre_o_codigo(texto)
    if not hits:
        return {
            "ok": False,
            "error": (
                f"No encontré '{texto}' en inventario. "
                "Prueba otro nombre o revisa cómo está en el maestro."
            ),
        }

    elegido = _elegir_mp_automatico(hits, bodega_origen)
    if elegido:
        info = _opciones_mp_desde_hits(hits, bodega_filtro=bodega_origen)
        nom_ok = next(
            (o["nombre_mp"] for o in info if o["cod_mp_sistema"] == elegido),
            elegido,
        )
        return {"ok": True, "cod_mp": elegido, "nombre_mp": nom_ok}

    opciones = _opciones_mp_desde_hits(hits, bodega_filtro=bodega_origen)
    if len(opciones) == 1:
        o = opciones[0]
        return {
            "ok": True,
            "cod_mp": o["cod_mp_sistema"],
            "nombre_mp": o["nombre_mp"],
        }

    lineas = [f"{o['indice']}. {o['texto_usuario']}" for o in opciones]
    return {
        "ok": False,
        "requiere_eleccion": True,
        "opciones": opciones,
        "mensaje": (
            "Hay varios productos parecidos. Pregunta al usuario cuál es "
            "(por nombre, sin pedir códigos MP):\n" + "\n".join(lineas)
        ),
    }


def _hint_fila_maestro_traslado(
    rows: list[dict], cod_mp: str, bodega: str, *, rol: str
) -> str:
    """Texto extra cuando falta fila MP×bodega en BD_MP_SISTEMA."""
    from bodegas_config import nombre_bodega, normalizar_cod_bodega

    filas = _filas_mp_maestro(rows, cod_mp)
    if not filas:
        return (
            f" No hay ninguna fila de MP {cod_mp} en el maestro. "
            "Verifica el código (ej. Buchanan's 18 = MP 566)."
        )
    por_bod: dict[str, float] = {}
    nombre = ""
    for r in filas:
        bod = normalizar_cod_bodega(r.get("cod_bodega"))
        if not bod:
            continue
        try:
            stk = float(r.get("stock_actual") or 0)
        except (TypeError, ValueError):
            stk = 0.0
        por_bod[bod] = por_bod.get(bod, 0.0) + stk
        if not nombre:
            nombre = (r.get("nombre_mp") or "").strip()
    desglose = ", ".join(
        f"{nombre_bodega(b)}={round(s, 2)}" for b, s in sorted(por_bod.items())
    )
    nom_txt = nombre or f"MP {cod_mp}"
    return (
        f" {nom_txt} sí está en inventario, pero no en "
        f"{nombre_bodega(bodega)} como {rol}. Stock por bodega: {desglose}. "
        "Si hay varios productos parecidos, pregunta al usuario cuál es "
        "(por nombre, no por código)."
    )


# ── Consumo teórico (migrado desde consultas_chat_extendidas) ─
def _hist_ventas_para_consumo(fecha_ini: str, fecha_fin: str) -> list[dict]:
    """
    Líneas de venta con campos para cruzar con recetas.
    Equivalente a consultas_chat_extendidas._hist_ventas_en_rango_para_consumo.
    """
    sb = conectar_supabase()
    sel = (
        "nombre_producto,cantidad_vendida,fecha,cod_receta,cod_smart_menu,"
        "cod_producto,variedad_smart_menu,estado_match"
    )
    try:
        sb.table("hist_ventas").select("estado_documento").limit(1).execute()
        sel += ",estado_documento"
    except Exception:
        pass

    out: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("hist_ventas")
            .select(sel)
            .gte("fecha", fecha_ini)
            .lte("fecha", fecha_fin)
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000

    return [
        r for r in out
        if not estado_documento_excluye_neto_operativo(r.get("estado_documento"))
    ]


def calcular_consumo_teorico_mp(fecha_ini: str, fecha_fin: str, cod_mp_sistema: str) -> dict:
    """
    Consumo teórico de un cod_mp_sistema cruzando hist_ventas (PROCESADO) con BD_RECETAS_DETALLE.
    Migrada desde consultas_chat_extendidas.calcular_consumo_teorico_mp.
    """
    from descargo_inventario import (
        _resolver_cod_receta,
        calcular_consumo,
        get_ingredientes,
    )
    from recetas_detalle import filtrar_solo_mp

    cod_target = (cod_mp_sistema or "").strip()
    if not cod_target:
        return {"error": "cod_mp_sistema vacío"}

    rows = _hist_ventas_para_consumo(fecha_ini, fecha_fin)
    if not rows:
        return {
            "error": "sin_lineas",
            "mensaje": f"No hay líneas en hist_ventas entre {fecha_ini} y {fecha_fin}.",
        }

    por_plato: dict[str, dict[str, float]] = {}
    lineas_procesadas = 0
    lineas_omitidas_match = 0
    lineas_sin_receta = 0
    lineas_sin_ingrediente_en_receta = 0

    for r in rows:
        em = (r.get("estado_match") or "").strip().upper()
        if em and em != "PROCESADO":
            lineas_omitidas_match += 1
            continue

        venta = {
            "cod_receta": r.get("cod_receta"),
            "cod_smart_menu": r.get("cod_smart_menu"),
            "cod_producto": r.get("cod_producto"),
            "variedad_smart_menu": r.get("variedad_smart_menu"),
        }
        cod_receta = _resolver_cod_receta(venta)
        variedad = r.get("variedad_smart_menu")
        cant_v = _to_float(r.get("cantidad_vendida"))
        nombre_plato = (r.get("nombre_producto") or "").strip() or "(sin nombre)"

        if not cod_receta:
            lineas_sin_receta += 1
            continue

        ingredientes = filtrar_solo_mp(get_ingredientes(cod_receta, variedad))
        if not ingredientes:
            lineas_sin_receta += 1
            continue

        subtotal_linea = 0.0
        for ing in ingredientes:
            c_mp = (ing.get("cod_mp_sistema") or "").strip()
            if c_mp != cod_target:
                continue
            subtotal_linea += calcular_consumo(ing, cant_v)

        if subtotal_linea <= 0:
            lineas_sin_ingrediente_en_receta += 1
            continue

        lineas_procesadas += 1
        acc = por_plato.setdefault(
            nombre_plato,
            {"unidades_vendidas": 0.0, "consumo_mp": 0.0},
        )
        acc["unidades_vendidas"] += cant_v
        acc["consumo_mp"] += subtotal_linea

    total = sum(x["consumo_mp"] for x in por_plato.values())
    suma_unidades_en_desglose = sum(x["unidades_vendidas"] for x in por_plato.values())
    desglose = sorted(por_plato.items(), key=lambda kv: kv[1]["consumo_mp"], reverse=True)

    return {
        "fecha_ini": fecha_ini,
        "fecha_fin": fecha_fin,
        "cod_mp_sistema": cod_target,
        "total_consumo_teorico": round(total, 4),
        "num_platos_en_desglose": len(por_plato),
        "suma_unidades_vendidas_en_desglose": round(suma_unidades_en_desglose, 4),
        "por_plato": [
            {
                "nombre_producto": nombre,
                "unidades_vendidas": round(d["unidades_vendidas"], 4),
                "consumo_mp": round(d["consumo_mp"], 4),
            }
            for nombre, d in desglose
        ],
        "lineas_hist_usadas": lineas_procesadas,
        "lineas_omitidas_match": lineas_omitidas_match,
        "lineas_sin_receta_o_vacia": lineas_sin_receta,
        "lineas_plato_sin_ese_mp": lineas_sin_ingrediente_en_receta,
        "nota": (
            "Consumo teórico según BD_RECETAS_DETALLE y ventas con matching PROCESADO; "
            "equivale a lo que el descargo de inventario descontaría por esas ventas. "
            "Cada nombre_producto en por_plato es el texto exacto de hist_ventas para esa línea "
            "agrupada: si aparece un plato que no reconoces (ej. postre), revisa ventas y la receta "
            "que enlaza ese producto con este cod_mp_sistema."
        ),
    }


# ── Paginación Supabase ──────────────────────────────────────
def supabase_query_all(sb, table, select, filters=None):
    """Lee todas las filas paginando de 1000 en 1000."""
    rows = []
    offset = 0
    while True:
        q = sb.table(table).select(select)
        if filters:
            for method, *args in filters:
                q = getattr(q, method)(*args)
        chunk = q.range(offset, offset + 999).execute().data
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


# ── FACTURAS / PENDIENTES ────────────────────────────────────
def tool_facturas_parciales(args=None):
    """Facturas con estado PARCIAL en Supabase (facturas_procesadas)."""
    args = args or {}
    limit, offset = _paging(args, default_limit=30, max_limit=200)
    sb = conectar_supabase()
    try:
        q = (
            sb.table("facturas_procesadas")
            .select("num_factura,ruc_proveedor,fecha_factura,items_procesados,items_sin_match,estado,fecha_proceso")
            .eq("estado", "PARCIAL")
            .order("fecha_factura", desc=True)
            .range(offset, offset + limit - 1)
        )
        data = q.execute().data or []
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}
    return {"ok": True, "total_en_pagina": len(data), "items": data, "paging": {"limit": limit, "offset": offset}}


def tool_items_pendientes_factura(args):
    """
    Lista filas PENDIENTE en BD_ITEMS_PENDIENTES por num_factura o por ruc_proveedor.
    args: {num_factura?: str, ruc_proveedor?: str, limit?: int, offset?: int}
    """
    args = args or {}
    num = str(args.get("num_factura", "") or "").strip()
    ruc = str(args.get("ruc_proveedor", "") or "").strip()
    limit, offset = _paging(args, default_limit=50, max_limit=200)

    if not num and not ruc:
        return {"ok": False, "error": "Debes enviar num_factura o ruc_proveedor", "items": []}

    headers, rows = leer_hoja_con_headers("BD_ITEMS_PENDIENTES", "clave_unica", skip_after_header=1)
    if not headers:
        return {"ok": False, "error": "No pude leer BD_ITEMS_PENDIENTES", "items": []}

    out = []
    for r in rows:
        estado = (r.get("estado") or "").strip().upper()
        if estado and estado != "PENDIENTE":
            continue
        if num and (r.get("num_factura") or "").strip() != num:
            continue
        if ruc and (r.get("ruc_proveedor") or "").strip() != ruc:
            continue
        out.append(
            {
                "num_factura": (r.get("num_factura") or "").strip(),
                "ruc_proveedor": (r.get("ruc_proveedor") or "").strip(),
                "cod_item_xml": (r.get("cod_item_xml") or "").strip(),
                "descripcion_xml": (r.get("descripcion_xml") or "").strip(),
                "cantidad": (r.get("cantidad") or "").strip(),
                "precio_unitario_xml": (r.get("precio_unitario_xml") or "").strip(),
                "costo_efectivo": (r.get("costo_efectivo") or "").strip(),
                "cod_mp_asignado": (r.get("cod_mp_asignado") or "").strip(),
                "nombre_mp_desde_bd": (r.get("nombre_mp_desde_bd") or "").strip(),
                "link_xml": (r.get("link_xml") or "").strip(),
            }
        )

    total = len(out)
    out = out[offset : offset + limit]
    return {"ok": True, "total": total, "items": out, "paging": {"limit": limit, "offset": offset}}


def _es_mov_compra_factura(r: dict) -> bool:
    """Misma lógica relajada que reporte_semanal: compras por factura."""
    t = (r.get("tipo_mov") or "").strip().upper()
    o = (r.get("origen_documento") or "").strip().upper()
    return t in ("ENTRADA", "ENTRADA_COMPRA") and (o == "FACTURA" or o == "")


def _solo_digitos_ruc(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def leer_bd_prov(*, force_refresh: bool = False) -> list[dict]:
    """Filas de BD_PROV con cod_proveedor, razon_social, ruc."""
    global _bd_prov_cache, _bd_prov_cache_at
    now = time.monotonic()
    if (
        not force_refresh
        and _bd_prov_cache is not None
        and (now - _bd_prov_cache_at) < BD_PROV_CACHE_TTL_SEC
    ):
        return _bd_prov_cache

    sh = conectar_sheets()
    vals = sh.worksheet("BD_PROV").get_all_values()
    if not vals:
        _bd_prov_cache = []
        _bd_prov_cache_at = now
        return []

    headers = [str(h or "").strip().lower() for h in vals[0]]
    idx = {h: i for i, h in enumerate(headers) if h}
    icod = idx.get("cod_proveedor", 0)
    irazon = idx.get("razon_social", 1)
    iruc = idx.get("ruc", idx.get("ruc_proveedor", 2))

    out: list[dict] = []
    for row in vals[1:]:
        if not any(str(c or "").strip() for c in row):
            continue
        cod = str(row[icod] if icod < len(row) else "").strip()
        razon = str(row[irazon] if irazon < len(row) else "").strip()
        ruc = _solo_digitos_ruc(str(row[iruc] if iruc < len(row) else ""))
        if not ruc and not razon:
            continue
        out.append(
            {
                "cod_proveedor": cod,
                "razon_social": razon,
                "ruc_proveedor": ruc,
            }
        )

    _bd_prov_cache = out
    _bd_prov_cache_at = now
    return out


def _mapa_ruc_razon_social(*, force_refresh: bool = False) -> dict[str, str]:
    """RUC (solo digitos) -> razon social desde BD_PROV y BD_ITEMS_PENDIENTES."""
    out: dict[str, str] = {}
    for p in leer_bd_prov(force_refresh=force_refresh):
        r = _solo_digitos_ruc(p.get("ruc_proveedor", ""))
        rz = (p.get("razon_social") or "").strip()
        if r and rz:
            out[r] = rz

    headers, rows = leer_hoja_con_headers(
        "BD_ITEMS_PENDIENTES", "clave_unica", skip_after_header=1
    )
    if headers:
        for row in rows:
            r = _solo_digitos_ruc(row.get("ruc_proveedor", ""))
            rz = (row.get("razon_social") or "").strip()
            if r and rz and r not in out:
                out[r] = rz
    return out


def _razon_desde_meta_factura(meta) -> str:
    if not meta:
        return ""
    if isinstance(meta, dict):
        return (meta.get("razon_social") or "").strip()
    return ""


def _coincide_nombre_proveedor(razon: str, filtro: str) -> bool:
    return _coincide_nombre_mp(razon, filtro)


def resolver_proveedor(
    *,
    nombre_proveedor: str = "",
    ruc_proveedor: str = "",
) -> dict:
    """
    Resuelve nombre o RUC a un proveedor en BD_PROV.
    Devuelve ok, ruc_proveedor, razon_social, candidatos[].
    """
    ruc_in = _solo_digitos_ruc(ruc_proveedor)
    nom_in = (nombre_proveedor or "").strip()
    provs = leer_bd_prov()

    if ruc_in:
        for p in provs:
            if p.get("ruc_proveedor") == ruc_in:
                return {
                    "ok": True,
                    "ruc_proveedor": ruc_in,
                    "razon_social": p.get("razon_social", ""),
                    "cod_proveedor": p.get("cod_proveedor", ""),
                }
        return {
            "ok": False,
            "error": f"No hay proveedor con RUC {ruc_in} en BD_PROV.",
            "candidatos": [],
        }

    if not nom_in:
        return {"ok": False, "error": "Indica nombre_proveedor o ruc_proveedor.", "candidatos": []}

    hits = [
        p
        for p in provs
        if _coincide_nombre_proveedor(p.get("razon_social", ""), nom_in)
    ]
    if len(hits) == 1:
        p = hits[0]
        return {
            "ok": True,
            "ruc_proveedor": p.get("ruc_proveedor", ""),
            "razon_social": p.get("razon_social", ""),
            "cod_proveedor": p.get("cod_proveedor", ""),
        }
    if len(hits) > 1:
        return {
            "ok": False,
            "error": "Varios proveedores coinciden; pide aclarar o usa el RUC.",
            "candidatos": [
                {
                    "razon_social": p.get("razon_social", ""),
                    "ruc_proveedor": p.get("ruc_proveedor", ""),
                }
                for p in hits[:8]
            ],
        }
    return {
        "ok": False,
        "error": f"No encontré proveedor con nombre parecido a '{nom_in}' en BD_PROV.",
        "candidatos": [],
    }


def _mapa_unidad_mp() -> dict[str, str]:
    return {
        str(r.get("cod_mp_sistema", "")).strip(): str(r.get("unidad_base", "")).strip().upper()
        for r in leer_bd_mp_sistema()
        if str(r.get("cod_mp_sistema", "")).strip()
    }


def _desc_xml_desde_obs(obs: str) -> str:
    obs = (obs or "").strip()
    if not obs:
        return ""
    return obs.split("|")[0].strip()


def _formatear_cantidad_compra(cantidad: float, unidad_base: str) -> dict:
    """cantidad_mov en inventario suele estar en unidad_base (GR, ML, UNI)."""
    u = (unidad_base or "GR").upper()
    cantidad = _to_float(cantidad, 0.0)
    out = {"cantidad_mov": round(cantidad, 4), "unidad_base": u}
    if u == "GR" and cantidad >= 1:
        out["cantidad_kg"] = round(cantidad / 1000.0, 4)
        out["cantidad_legible"] = f"{out['cantidad_kg']:.2f} kg ({cantidad:.0f} g)"
    elif u == "ML" and cantidad >= 1:
        out["cantidad_litros"] = round(cantidad / 1000.0, 4)
        out["cantidad_legible"] = f"{out['cantidad_litros']:.2f} L ({cantidad:.0f} ml)"
    else:
        out["cantidad_legible"] = f"{cantidad:.2f} {u}"
    return out


def _lineas_compra_factura(sb, num_factura: str) -> list[dict]:
    res = (
        sb.table("mov_inventario")
        .select(
            "fecha,cod_mp_sistema,nombre_mp,cantidad_mov,costo_unitario,costo_total,observaciones,tipo_mov,origen_documento"
        )
        .eq("num_documento", num_factura.strip())
        .execute()
    )
    unidades = _mapa_unidad_mp()
    lineas = []
    for r in res.data or []:
        if not _es_mov_compra_factura(r):
            continue
        cod = (r.get("cod_mp_sistema") or "").strip()
        cant = _to_float(r.get("cantidad_mov"), 0.0)
        ct = _to_float(r.get("costo_total"), 0.0)
        cu = _to_float(r.get("costo_unitario"), 0.0)
        ub = unidades.get(cod, "GR")
        fmt = _formatear_cantidad_compra(cant, ub)
        desc_xml = _desc_xml_desde_obs(r.get("observaciones") or "")
        precio_kg = None
        if ub == "GR" and cant > 0 and ct > 0:
            precio_kg = round((ct / cant) * 1000.0, 4)
        lineas.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": (r.get("nombre_mp") or cod).strip(),
                "descripcion_xml": desc_xml,
                **fmt,
                "costo_unitario": round(cu, 6),
                "costo_total_usd": round(ct, 2),
                "precio_usd_por_kg": precio_kg,
                "fecha_mov": (str(r.get("fecha") or "")[:10]),
            }
        )
    return lineas


def _texto_whatsapp_compra_factura(
    *,
    num_factura: str,
    razon_social: str,
    ruc_proveedor: str,
    fecha_factura: str,
    lineas: list[dict],
) -> str:
    total = round(sum(_to_float(x.get("costo_total_usd"), 0) for x in lineas), 2)
    prov = razon_social or ruc_proveedor or "?"
    lines = [
        f"Factura {num_factura} ({fecha_factura or '?'})",
        f"Proveedor: {prov}" + (f" RUC {ruc_proveedor}" if ruc_proveedor else ""),
        "Lineas ingresadas a inventario:",
    ]
    for i, ln in enumerate(lineas, 1):
        nom = ln.get("nombre_mp") or "?"
        leg = ln.get("cantidad_legible") or "?"
        usd = ln.get("costo_total_usd", 0)
        desc = ln.get("descripcion_xml") or ""
        lines.append(f"{i}. {nom} ({ln.get('cod_mp_sistema','')}) — {leg} — {usd:.2f} USD")
        if desc and desc.upper() != nom.upper():
            lines.append(f"   XML: {desc[:120]}")
    lines.append(f"Total factura: {total:.2f} USD")
    return "\n".join(lines)


def _etiqueta_proveedor_compras(
    ruc: str,
    razon: str,
    *,
    facturas_ejemplo: list[str] | None = None,
) -> str:
    ruc = _solo_digitos_ruc(ruc)
    if razon:
        return f"{razon} ({ruc})" if ruc else razon
    if not ruc:
        return "(proveedor desconocido)"
    hint = ""
    if facturas_ejemplo:
        hint = f"; factura ej. {facturas_ejemplo[0]}"
    return f"RUC {ruc} (sin nombre en BD_PROV{hint})"


def _texto_whatsapp_compras_rango(
    *,
    periodo_label: str,
    fecha_desde: str,
    fecha_hasta: str,
    resumen: dict,
    por_proveedor: list[dict],
) -> str:
    lines = [
        f"Compras ingresadas a inventario — {periodo_label} ({fecha_desde} al {fecha_hasta}):",
        "",
        "Que incluye este total:",
        "- Solo lineas ya registradas en inventario (mov_inventario ENTRADA por factura).",
        "- Monto = costo_total de cada linea de MP ingresada, no el total del XML si hubo lineas sin match.",
        "- No incluye servicios, gastos no inventariables ni lineas pendientes sin procesar.",
        "",
        f"Total ingresado a inventario: {float(resumen.get('total_compras_usd', 0)):.2f} USD",
        f"{resumen.get('n_facturas_distintas', 0)} facturas / "
        f"{resumen.get('n_lineas_movimiento', 0)} lineas / "
        f"{resumen.get('n_productos_mp_distintos', 0)} MPs distintos",
    ]
    parc = int(resumen.get("n_facturas_parciales") or 0)
    sin_match = int(resumen.get("items_sin_match_total") or 0)
    if parc or sin_match:
        lines.append(
            f"Nota: {parc} factura(s) PARCIAL con {sin_match} linea(s) sin match "
            "(esas lineas no suman aqui hasta ingresarse)."
        )
    sin_nom = resumen.get("proveedores_sin_nombre") or []
    if sin_nom:
        lines.append(
            "Proveedores sin razon social en BD_PROV: "
            + ", ".join(str(x) for x in sin_nom[:8])
            + ("..." if len(sin_nom) > 8 else "")
            + " (agregalos en BD_PROV o revisa la factura ejemplo)."
        )
    lines.append("")
    lines.append("Gasto por proveedor (solo lo ingresado a inventario):")
    for i, p in enumerate(por_proveedor, 1):
        lines.append(
            f"{i}. {_etiqueta_proveedor_compras(p.get('ruc_proveedor', ''), p.get('razon_social', ''), facturas_ejemplo=p.get('facturas_ejemplo'))}: "
            f"{float(p.get('total_usd', 0)):.2f} USD"
        )
    lines.append("")
    lines.append("Fuente: mov_inventario ENTRADA + facturas_procesadas (RUC).")
    return "\n".join(lines)


def tool_compras_factura_detalle(args):
    """
    Lineas exactas de una factura de compra en mov_inventario.
    num_factura O (nombre_proveedor/ruc + ultima=true por fecha de factura).
    """
    args = args or {}
    num = str(args.get("num_factura") or "").strip()
    ultima = bool(args.get("ultima"))
    nom = str(args.get("nombre_proveedor") or "").strip()
    ruc = str(args.get("ruc_proveedor") or "").strip()

    sb = conectar_supabase()

    if not num and ultima:
        res_prov = resolver_proveedor(nombre_proveedor=nom, ruc_proveedor=ruc)
        if not res_prov.get("ok"):
            return {"ok": False, **res_prov}
        ruc_f = res_prov["ruc_proveedor"]
        fp = (
            sb.table("facturas_procesadas")
            .select("num_factura,ruc_proveedor,fecha_factura,estado")
            .eq("ruc_proveedor", ruc_f)
            .order("fecha_factura", desc=True)
            .limit(1)
            .execute()
        )
        if not fp.data:
            return {
                "ok": False,
                "error": f"Sin facturas procesadas para RUC {ruc_f}.",
            }
        row = fp.data[0]
        num = (row.get("num_factura") or "").strip()
        meta_fp = row
    elif not num:
        return {
            "ok": False,
            "error": "Indica num_factura o ultima=true con nombre_proveedor/ruc_proveedor.",
        }
    else:
        meta_fp = None

    lineas = _lineas_compra_factura(sb, num)
    if not lineas:
        return {
            "ok": False,
            "error": f"No hay entradas de inventario para la factura {num}.",
            "num_factura": num,
        }

    ruc_doc = ruc
    fecha_fac = ""
    if meta_fp:
        ruc_doc = meta_fp.get("ruc_proveedor") or ruc_doc
        fecha_fac = (meta_fp.get("fecha_factura") or "")[:10]
    else:
        try:
            fp2 = (
                sb.table("facturas_procesadas")
                .select("ruc_proveedor,fecha_factura")
                .eq("num_factura", num)
                .limit(1)
                .execute()
            )
            if fp2.data:
                ruc_doc = fp2.data[0].get("ruc_proveedor") or ruc_doc
                fecha_fac = (fp2.data[0].get("fecha_factura") or "")[:10]
        except Exception:
            pass

    razon = ""
    if ruc_doc:
        for p in leer_bd_prov():
            if p.get("ruc_proveedor") == _solo_digitos_ruc(ruc_doc):
                razon = p.get("razon_social", "")
                break
    if not razon and nom:
        rp = resolver_proveedor(nombre_proveedor=nom, ruc_proveedor=ruc_doc)
        if rp.get("ok"):
            razon = rp.get("razon_social", "")

    if not fecha_fac:
        fecha_fac = lineas[0].get("fecha_mov") or ""

    texto = _texto_whatsapp_compra_factura(
        num_factura=num,
        razon_social=razon,
        ruc_proveedor=_solo_digitos_ruc(ruc_doc),
        fecha_factura=fecha_fac,
        lineas=lineas,
    )
    return {
        "ok": True,
        "num_factura": num,
        "ruc_proveedor": _solo_digitos_ruc(ruc_doc),
        "razon_social": razon,
        "fecha_factura": fecha_fac,
        "lineas": lineas,
        "total_usd": round(sum(x["costo_total_usd"] for x in lineas), 2),
        "texto_whatsapp": texto,
        "nota": "Usa texto_whatsapp tal cual. Nombres = nombre_mp del sistema; descripcion_xml es del XML del proveedor.",
    }


def tool_compras_facturas_rango(args):
    """
    Compras desde facturas ya registradas en inventario: mov_inventario
    (ENTRADA / ENTRADA_COMPRA por factura) entre dos fechas inclusive.
    Devuelve totales, top facturas, top productos (MP) y agregado por proveedor (RUC).
    """
    args = args or {}
    desde = str(args.get("fecha_desde") or "").strip()
    hasta = str(args.get("fecha_hasta") or "").strip()
    if not desde or not hasta:
        try:
            from ventas_resumen_tools import resolver_rango_fechas

            fi, ff, _ = resolver_rango_fechas(args)
            desde, hasta = fi, ff
        except ValueError:
            pass
    top_facturas = min(max(int(args.get("top_facturas", 40) or 40), 5), 100)
    top_productos = min(max(int(args.get("top_productos", 35) or 35), 5), 100)
    filtro_ruc = _solo_digitos_ruc(str(args.get("ruc_proveedor") or ""))
    filtro_nom = str(args.get("nombre_proveedor") or "").strip()

    if filtro_nom and not filtro_ruc:
        rp = resolver_proveedor(nombre_proveedor=filtro_nom)
        if not rp.get("ok"):
            return {"ok": False, **rp}
        filtro_ruc = rp["ruc_proveedor"]
        filtro_razon = rp.get("razon_social", "")
    else:
        filtro_razon = ""
        if filtro_ruc:
            rp = resolver_proveedor(ruc_proveedor=filtro_ruc)
            if rp.get("ok"):
                filtro_razon = rp.get("razon_social", "")

    if not desde or not hasta:
        return {
            "ok": False,
            "error": "Obligatorio: fecha_desde y fecha_hasta como YYYY-MM-DD (ej. 2026-05-01).",
        }

    try:
        d0 = datetime.strptime(desde, "%Y-%m-%d").date()
        d1 = datetime.strptime(hasta, "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "error": "Fechas invalidas. Usa formato YYYY-MM-DD."}

    if d0 > d1:
        return {"ok": False, "error": "fecha_desde no puede ser posterior a fecha_hasta."}

    if (d1 - d0).days > 400:
        return {"ok": False, "error": "Rango maximo 400 dias. Acorta el periodo."}

    sb = conectar_supabase()
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("mov_inventario")
            .select(
                "fecha,tipo_mov,origen_documento,num_documento,cod_mp_sistema,nombre_mp,cantidad_mov,costo_total"
            )
            .gte("fecha", f"{desde}T00:00:00")
            .lte("fecha", f"{hasta}T23:59:59")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        rows.extend(r for r in chunk if _es_mov_compra_factura(r))
        if len(chunk) < 1000:
            break
        offset += 1000

    if not rows:
        vacio = {
            "fecha_desde": desde,
            "fecha_hasta": hasta,
            "total_compras_usd": 0.0,
            "n_lineas_movimiento": 0,
            "n_facturas_distintas": 0,
            "n_productos_mp_distintos": 0,
            "tipo_monto": "solo_lineas_ingresadas_inventario",
            "nota": "Sin lineas ENTRADA por factura en mov_inventario en ese rango.",
        }
        return {
            "ok": True,
            "resumen": vacio,
            "por_proveedor": [],
            "top_facturas": [],
            "top_productos": [],
            "texto_whatsapp": (
                f"Compras ingresadas a inventario ({desde} al {hasta}): "
                "sin lineas ENTRADA por factura en mov_inventario en ese periodo."
            ),
        }

    by_doc: dict[str, dict] = {}
    by_mp: dict[str, dict] = defaultdict(
        lambda: {"nombre_mp": "", "cantidad": 0.0, "costo_total": 0.0}
    )
    total = 0.0

    for r in rows:
        ct = _to_float(r.get("costo_total"), 0.0)
        total += ct
        num = (r.get("num_documento") or "").strip() or "(sin_num_documento)"
        fe = (str(r.get("fecha") or "")[:10]) or desde

        if num not in by_doc:
            by_doc[num] = {
                "num_factura": num,
                "fecha_primera": fe,
                "total_usd": 0.0,
                "n_lineas": 0,
            }
        else:
            if fe < by_doc[num]["fecha_primera"]:
                by_doc[num]["fecha_primera"] = fe
        by_doc[num]["total_usd"] += ct
        by_doc[num]["n_lineas"] += 1

        cod = (r.get("cod_mp_sistema") or "").strip()
        if cod:
            mp = by_mp[cod]
            nom = (r.get("nombre_mp") or "").strip()
            if nom:
                mp["nombre_mp"] = nom
            mp["cantidad"] += _to_float(r.get("cantidad_mov"), 0.0)
            mp["costo_total"] += ct

    nums = sorted({n for n in by_doc if n != "(sin_num_documento)"})
    ruc_map: dict[str, str] = {}
    ruc_razon_meta: dict[str, str] = {}
    n_facturas_parciales = 0
    items_sin_match_total = 0
    meta_by_num: dict[str, dict] = {}
    for i in range(0, len(nums), 80):
        part = nums[i : i + 80]
        try:
            res = (
                sb.table("facturas_procesadas")
                .select(
                    "num_factura,ruc_proveedor,meta,estado,items_sin_match,fecha_factura"
                )
                .in_("num_factura", part)
                .execute()
            )
            for x in res.data or []:
                n = (x.get("num_factura") or "").strip()
                if not n:
                    continue
                ruc_map[n] = (x.get("ruc_proveedor") or "").strip()
                meta_raw = x.get("meta")
                if isinstance(meta_raw, dict):
                    meta_by_num[n] = meta_raw
                ruc_k = _solo_digitos_ruc(x.get("ruc_proveedor", ""))
                rz_meta = _razon_desde_meta_factura(meta_raw)
                if ruc_k and rz_meta and ruc_k not in ruc_razon_meta:
                    ruc_razon_meta[ruc_k] = rz_meta
                if (x.get("estado") or "").strip().upper() == "PARCIAL":
                    n_facturas_parciales += 1
                items_sin_match_total += int(x.get("items_sin_match") or 0)
        except Exception:
            pass

    from proveedor_favorita import es_ruc_favorita, resolver_cod_proveedor_factura

    cod_nombre = {
        str(p.get("cod_proveedor", "")).strip(): str(p.get("razon_social", "")).strip()
        for p in leer_bd_prov()
        if p.get("cod_proveedor")
    }

    ruc_nombre = _mapa_ruc_razon_social()
    for rk, rz in ruc_razon_meta.items():
        if rk and rz and rk not in ruc_nombre:
            ruc_nombre[rk] = rz

    facturas_por_ruc: dict[str, list[str]] = defaultdict(list)
    for d in by_doc.values():
        n = d["num_factura"]
        ruc = ruc_map.get(n, "") if n != "(sin_num_documento)" else ""
        d["ruc_proveedor"] = ruc
        ruc_k = _solo_digitos_ruc(ruc)
        d["razon_social"] = ruc_nombre.get(ruc_k, "")
        if ruc_k and n != "(sin_num_documento)":
            facturas_por_ruc[ruc_k].append(n)

    doc_vals = list(by_doc.values())
    if filtro_ruc:
        doc_vals = [
            d
            for d in doc_vals
            if _solo_digitos_ruc(d.get("ruc_proveedor", "")) == filtro_ruc
        ]
        rows = [
            r
            for r in rows
            if _solo_digitos_ruc(ruc_map.get((r.get("num_documento") or "").strip(), ""))
            == filtro_ruc
        ]
        by_mp = defaultdict(
            lambda: {"nombre_mp": "", "cantidad": 0.0, "costo_total": 0.0}
        )
        total = 0.0
        for r in rows:
            ct = _to_float(r.get("costo_total"), 0.0)
            total += ct
            cod = (r.get("cod_mp_sistema") or "").strip()
            if cod:
                mp = by_mp[cod]
                nom = (r.get("nombre_mp") or "").strip()
                if nom:
                    mp["nombre_mp"] = nom
                mp["cantidad"] += _to_float(r.get("cantidad_mov"), 0.0)
                mp["costo_total"] += ct

    por_prov: dict[str, float] = defaultdict(float)
    prov_info: dict[str, dict] = {}
    for d in doc_vals:
        n = d["num_factura"]
        ruc = (d.get("ruc_proveedor") or "").strip()
        meta_fp = meta_by_num.get(n) or {}
        if es_ruc_favorita(ruc):
            cod = (meta_fp.get("cod_proveedor") or "").strip() or resolver_cod_proveedor_factura(
                ruc, n
            )
            clave = f"cod:{cod}"
            por_prov[clave] += d["total_usd"]
            if clave not in prov_info:
                prov_info[clave] = {
                    "cod_proveedor": cod,
                    "ruc_proveedor": ruc,
                    "razon_social": cod_nombre.get(cod, cod),
                }
        else:
            clave = ruc if ruc else f"sin_ruc:{n}"
            por_prov[clave] += d["total_usd"]
            if clave not in prov_info:
                ruc_k = _solo_digitos_ruc(ruc)
                prov_info[clave] = {
                    "cod_proveedor": "",
                    "ruc_proveedor": ruc_k or ruc,
                    "razon_social": ruc_nombre.get(ruc_k, ""),
                }

    top_f = sorted(doc_vals, key=lambda x: x["total_usd"], reverse=True)[:top_facturas]
    for x in top_f:
        x["total_usd"] = round(x["total_usd"], 2)

    prod_list = []
    for cod, v in by_mp.items():
        prod_list.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": v["nombre_mp"] or cod,
                "cantidad_mov_total": round(v["cantidad"], 4),
                "costo_total_usd": round(v["costo_total"], 2),
            }
        )
    prod_list.sort(key=lambda x: x["costo_total_usd"], reverse=True)
    prod_list = prod_list[:top_productos]

    prov_out = []
    proveedores_sin_nombre: list[str] = []
    facturas_por_clave: dict[str, list[str]] = defaultdict(list)
    for d in doc_vals:
        n = d["num_factura"]
        ruc = (d.get("ruc_proveedor") or "").strip()
        meta_fp = meta_by_num.get(n) or {}
        if es_ruc_favorita(ruc):
            cod = (meta_fp.get("cod_proveedor") or "").strip() or resolver_cod_proveedor_factura(
                ruc, n
            )
            facturas_por_clave[f"cod:{cod}"].append(n)
        elif ruc:
            facturas_por_clave[ruc].append(n)

    for k, v in sorted(por_prov.items(), key=lambda kv: -kv[1])[:30]:
        info = prov_info.get(k, {})
        ruc_k = _solo_digitos_ruc(info.get("ruc_proveedor", ""))
        razon = info.get("razon_social") or (ruc_nombre.get(ruc_k, "") if ruc_k else "")
        fac_ej = sorted(facturas_por_clave.get(k, []))[:3]
        if ruc_k and not razon:
            proveedores_sin_nombre.append(ruc_k)
        prov_out.append(
            {
                "proveedor_clave": k,
                "cod_proveedor": info.get("cod_proveedor", ""),
                "ruc_proveedor": ruc_k or info.get("ruc_proveedor", ""),
                "razon_social": razon,
                "facturas_ejemplo": fac_ej,
                "total_usd": round(v, 2),
            }
        )

    periodo_label = desde
    if desde != hasta:
        try:
            from ventas_resumen_tools import resolver_rango_fechas

            _, _, periodo_label = resolver_rango_fechas(
                {"fecha_ini": desde, "fecha_fin": hasta}
            )
        except ValueError:
            periodo_label = f"{desde} al {hasta}"

    resumen = {
        "fecha_desde": desde,
        "fecha_hasta": hasta,
        "periodo_label": periodo_label,
        "total_compras_usd": round(total, 2),
        "n_lineas_movimiento": len(rows),
        "n_facturas_distintas": len(doc_vals),
        "n_productos_mp_distintos": len(by_mp),
        "n_facturas_parciales": n_facturas_parciales,
        "items_sin_match_total": items_sin_match_total,
        "proveedores_sin_nombre": proveedores_sin_nombre,
        "tipo_monto": "solo_lineas_ingresadas_inventario",
        "que_incluye": (
            "Suma de costo_total en mov_inventario (ENTRADA por factura): materias primas "
            "ya ingresadas al inventario."
        ),
        "que_no_incluye": (
            "Total del XML de la factura si hubo lineas sin match, servicios no inventariables "
            "o conceptos no procesados a mov_inventario."
        ),
        "nota": (
            "Montos = costo_total por linea en mov_inventario ENTRADA. "
            "Razon social desde BD_PROV, BD_ITEMS_PENDIENTES o meta de factura. "
            "Para lineas de UNA factura usa compras_factura_detalle."
        ),
    }
    if filtro_ruc:
        resumen["filtro_proveedor"] = {
            "ruc_proveedor": filtro_ruc,
            "razon_social": filtro_razon or ruc_nombre.get(filtro_ruc, ""),
        }

    texto = _texto_whatsapp_compras_rango(
        periodo_label=periodo_label,
        fecha_desde=desde,
        fecha_hasta=hasta,
        resumen=resumen,
        por_proveedor=prov_out,
    )
    return {
        "ok": True,
        "resumen": resumen,
        "por_proveedor": prov_out,
        "top_facturas": top_f,
        "top_productos": prod_list,
        "texto_whatsapp": texto,
    }


def tool_mp_incompletas(args=None):
    """
    MPs con datos incompletos en BD_MP_SISTEMA.
    args:
      - tipo: 'sin_costo' | 'sin_par' | 'sin_bodega'
      - limit/offset
    """
    args = args or {}
    tipo = str(args.get("tipo", "sin_costo") or "sin_costo").strip().lower()
    limit, offset = _paging(args, default_limit=50, max_limit=200)

    rows = leer_bd_mp_sistema()
    out = []
    for r in rows:
        cod = str(r.get("cod_mp_sistema", "")).strip()
        if not cod:
            continue
        nombre = str(r.get("nombre_mp", cod)).strip()
        unidad = str(r.get("unidad_base", "")).strip()
        bod = str(r.get("cod_bodega", "")).strip()
        stock = _to_float(r.get("stock_actual", 0), 0.0)
        par = _to_float(r.get("par_level", 0), 0.0)
        costo = _to_float(r.get("costo_unitario_ref", 0), 0.0)

        match = False
        if tipo == "sin_par":
            match = par <= 0
        elif tipo == "sin_bodega":
            match = bod == ""
        else:  # sin_costo
            match = costo <= 0
        if not match:
            continue

        out.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": nombre,
                "stock_actual": round(stock, 4),
                "unidad": unidad,
                "par_level": round(par, 4),
                "costo_unitario_ref": round(costo, 6),
                "cod_bodega": bod,
            }
        )

    total = len(out)
    # Orden útil: sin_costo por stock_abs desc, sin_par por stock_abs desc, sin_bodega por nombre
    if tipo in {"sin_costo", "sin_par"}:
        out.sort(key=lambda x: abs(x["stock_actual"]), reverse=True)
    else:
        out.sort(key=lambda x: x["nombre_mp"])

    out = out[offset : offset + limit]
    return {"tipo": tipo, "total": total, "items": out, "paging": {"limit": limit, "offset": offset}}


def tool_resumen_operativo_hoy(args=None):
    """
    Resumen compacto para WhatsApp:
      - ventas hoy (total oficial + tickets)
      - bajo par (conteo + top N)
      - negativos (conteo + top N)
      - facturas parciales (conteo página)
    args:
      - top: int (default 10) para listados internos
    """
    args = args or {}
    top = _to_int(args.get("top", 10), 10)
    top = _clamp(top, 3, 30)

    ventas = tool_ventas_hoy()
    bajo_par = tool_stock_critico({"top": top})
    neg = tool_stocks_negativos({"top": top})
    parc = tool_facturas_parciales({"limit": 20, "offset": 0})

    return {
        "fecha": ventas.get("fecha"),
        "ventas": {"total_ventas": ventas.get("total_ventas"), "tickets": ventas.get("tickets"), "fuente": ventas.get("fuente", None) or ventas.get("nota", None)},
        "bajo_par": {"total": bajo_par.get("total_bajo_par"), "items": bajo_par.get("items", [])},
        "negativos": {"total": neg.get("total_negativos"), "items": neg.get("items", [])},
        "facturas_parciales": {"ok": parc.get("ok"), "total_en_pagina": parc.get("total_en_pagina"), "items": parc.get("items", [])},
    }


def _fecha_hoy_ec() -> date:
    return datetime.now(TZ).date()


def _neto_linea_hist_ventas(row: dict) -> float:
    """Neto por línea: subtotal − descuento, o columna total si faltan las anteriores."""
    sub = _to_float(row.get("subtotal"), 0.0)
    desc = _to_float(row.get("descuento_valor"), 0.0)
    if sub > 0 or desc > 0:
        return sub - desc
    return _to_float(row.get("total"), 0.0)


def _smartmenu_dia_valido(sm: dict | None) -> bool:
    """True si Smart Menu devolvió totales reales (no grid vacío por red)."""
    if not sm:
        return False
    return (sm.get("docs") or 0) > 0 or (sm.get("total") or 0) > 0.01


def _hist_ventas_sin_anulados(rows):
    """Excluye líneas de documentos anulados (hist_ventas.estado_documento = ANULADO)."""
    return [
        r
        for r in rows
        if not estado_documento_excluye_neto_operativo(r.get("estado_documento"))
    ]

# ── Total oficial via Smart Menu ─────────────────────────────
def total_smartmenu_dia(fecha_str):
    """Totales Smart Menu del día: dict con total (neto), total_bruto, total_descuentos, docs."""
    try:
        import importlib.util, sys
        # Importar dinámicamente desde el mismo directorio
        spec = importlib.util.spec_from_file_location(
            "ventas_smartmenu_total",
            os.path.join(os.path.dirname(__file__), "ventas_smartmenu_total.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.calcular_total_smartmenu(fecha_str, sin_iva=True)
    except Exception:
        return None  # fallback: indica que Smart Menu no disponible
    if not _smartmenu_dia_valido(result):
        return None
    return result


def _total_desde_hist_ventas_docs(fecha_desde: str, fecha_hasta: str) -> tuple[float, int]:
    """Lee total y número de documentos desde hist_ventas cuando Smart Menu no está disponible."""
    sb = conectar_supabase()
    rows = supabase_query_all(
        sb, "hist_ventas",
        "num_documento,total,subtotal,descuento_valor,estado_documento",
        [("gte", "fecha", fecha_desde), ("lte", "fecha", fecha_hasta)]
    )
    rows = _hist_ventas_sin_anulados(rows)
    docs = set(r.get("num_documento") for r in rows if r.get("num_documento"))
    total = sum(_neto_linea_hist_ventas(r) for r in rows)
    return round(total, 2), len(docs)


def _totales_por_dia_hist(fecha_desde: str, fecha_hasta: str) -> dict[str, tuple[float, int]]:
    """Totales netos y tickets por fecha desde hist_ventas (una sola consulta)."""
    sb = conectar_supabase()
    rows = supabase_query_all(
        sb,
        "hist_ventas",
        "fecha,num_documento,subtotal,descuento_valor,estado_documento",
        [("gte", "fecha", fecha_desde), ("lte", "fecha", fecha_hasta)],
    )
    rows = _hist_ventas_sin_anulados(rows)
    por_fecha: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "docs": set()})
    for r in rows:
        f = (r.get("fecha") or "")[:10]
        if not f:
            continue
        por_fecha[f]["total"] += _neto_linea_hist_ventas(r)
        doc = (r.get("num_documento") or "").strip()
        if doc:
            por_fecha[f]["docs"].add(doc)
    return {
        f: (round(v["total"], 2), len(v["docs"]))
        for f, v in por_fecha.items()
    }


# ── TOOL 1 — ventas hoy ─────────────────────────────────────
def tool_ventas_hoy():
    hoy = _fecha_hoy_ec().isoformat()
    sm = total_smartmenu_dia(hoy)

    # Top platos desde hist_ventas (orden por monto total del día)
    sb = conectar_supabase()
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,total,estado_documento",
        [("eq", "fecha", hoy)])
    rows = _hist_ventas_sin_anulados(rows)
    conteo = defaultdict(lambda: {"cantidad": 0, "total": 0})
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(sin nombre)"
        conteo[nombre]["cantidad"] += _to_float(r.get("cantidad_vendida"), 0)
        conteo[nombre]["total"] += _to_float(r.get("total"), 0.0)
    top5 = sorted(conteo.items(), key=lambda x: x[1]["total"], reverse=True)[:5]

    resultado = {
        "fecha": hoy,
        "top_platos": [
            {"plato": n, "cantidad": int(d["cantidad"]), "total_usd": round(d["total"], 2)}
            for n, d in top5
        ],
    }
    if _smartmenu_dia_valido(sm):
        resultado["total_ventas"] = round(sm.get("total", 0), 2)
        resultado["ventas_brutas"] = round(sm.get("total_bruto", sm.get("total", 0)), 2)
        resultado["descuentos"] = round(sm.get("total_descuentos", 0), 2)
        resultado["tickets"] = sm.get("docs", 0)
        resultado["fuente"] = "Smart Menu"
    else:
        total_hv, docs_hv = _total_desde_hist_ventas_docs(hoy, hoy)
        resultado["total_ventas"] = total_hv
        resultado["tickets"] = docs_hv
        resultado["fuente"] = "hist_ventas"
    return resultado

# ── TOOL 2 — ventas semana ──────────────────────────────────
def tool_ventas_semana():
    hoy = _fecha_hoy_ec()
    lunes = hoy - timedelta(days=hoy.weekday())

    # Total oficial: suma diaria via Smart Menu
    total_oficial = 0
    total_docs = 0
    sm_disponible = True
    d = lunes
    while d <= hoy:
        sm = total_smartmenu_dia(d.isoformat())
        if sm is None:
            sm_disponible = False
            break
        total_oficial += sm.get("total", 0)
        total_docs += (sm.get("docs") or 0)
        d += timedelta(days=1)

    # Fallback y ranking desde hist_ventas
    sb = conectar_supabase()
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,total,fecha,estado_documento",
        [("gte", "fecha", lunes.isoformat()), ("lte", "fecha", hoy.isoformat())])
    rows = _hist_ventas_sin_anulados(rows)

    dias = len(set(r["fecha"] for r in rows))
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]

    if not sm_disponible:
        total_oficial, total_docs = _total_desde_hist_ventas_docs(lunes.isoformat(), hoy.isoformat())

    return {
        "periodo": f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
        "total_ventas": round(total_oficial, 2),
        "dias_activos": dias,
        "tickets": total_docs,
        "promedio_diario": round(total_oficial/dias, 2) if dias else 0,
        "top_platos": [{"plato": n, "cantidad": int(c)} for n,c in top5],
        "fuente": "Smart Menu" if sm_disponible else "hist_ventas (aproximado)"
    }

# ── TOOL 3 — stock bajo par level ────────────────────────────
# Importante: NO truncar a top10 a menos que el usuario lo pida.
def tool_stock_critico(args=None):
    """
    Lista MPs bajo par level (par_level > 0 y stock_actual < par_level).
    args opcional: {"top": int} para truncar.
    """
    args = args or {}
    top = args.get("top", 0)
    try:
        top = int(top) if top else 0
    except Exception:
        top = 0

    from inventario_stock_mp import mps_bajo_par

    rows = leer_bd_mp_sistema()
    criticos = []
    for cod, info in mps_bajo_par(rows).items():
        par = float(info["par_level"])
        stock = float(info["stock_total"])
        criticos.append({
            "cod_mp_sistema": cod,
            "nombre": info["nombre_mp"],
            "stock_actual": round(stock, 1),
            "stock_por_bodega": info["por_bodega"],
            "par_level": round(par, 1),
            "unidad": info["unidad_base"],
            "deficit_pct": round((1 - stock / par) * 100, 1) if par > 0 else 0.0,
        })
    criticos.sort(key=lambda x: x["deficit_pct"], reverse=True)
    total = len(criticos)
    if top and top > 0:
        criticos = criticos[:top]
    return {"total_bajo_par": total, "items": criticos}


# ── TOOL 3B — stocks negativos (NO inventar) ─────────────────
def tool_stocks_negativos(args=None):
    """
    Lista MPs con stock_actual < 0 desde BD_MP_SISTEMA.
    args opcional: {"top": int} para truncar.
    """
    args = args or {}
    top = args.get("top", 0)
    try:
        top = int(top) if top else 0
    except Exception:
        top = 0

    rows = leer_bd_mp_sistema()
    negativos = []
    for r in rows:
        cod = str(r.get("cod_mp_sistema", "")).strip()
        nombre = str(r.get("nombre_mp", cod)).strip()
        unidad = str(r.get("unidad_base", "")).strip()
        try:
            stock = _to_float(r.get("stock_actual", "0") or "0")
        except Exception:
            continue
        if stock < 0:
            negativos.append(
                {
                    "cod_mp_sistema": cod,
                    "nombre_mp": nombre,
                    "stock_actual": round(stock, 4),
                    "unidad": unidad,
                }
            )
    negativos.sort(key=lambda x: x["stock_actual"])  # más negativo primero
    total = len(negativos)
    if top and top > 0:
        negativos = negativos[:top]
    return {"total_negativos": total, "items": negativos}

# ── TOOL 3C — inventario valorizado (USD) ────────────────────
def tool_inventario_valorizado(args=None):
    """
    Valorización usando BD_MP_SISTEMA: stock_actual * costo_unitario_ref.
    args opcional:
      - nombre_mp o buscar: texto para filtrar por nombre_mp (parcial, sin importar tildes/plural).
      - cod_bodega: filtra por bodega
      - top: int (top por valor absoluto)
      - incluir_cero: bool (default False)
      - incluir_negativos: bool (default False). Si False, negativos se tratan como 0 para valorización.
      - limit/offset: paginación del listado (si no usas top)

    Si nombre_mp está definido: devuelve solo MPs que coincidan e incluye filas sin costo o con stock 0
    (para que el usuario vea «existe pero no valoriza»).
    """
    args = args or {}
    cod_bod = str(args.get("cod_bodega", "") or "").strip()
    buscar = str(args.get("nombre_mp") or args.get("buscar") or "").strip()
    incluir_cero = str(args.get("incluir_cero", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}
    incluir_negativos = str(args.get("incluir_negativos", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}
    top = _to_int(args.get("top", 0), 0)
    limit, offset = _paging(args, default_limit=50, max_limit=200)

    rows = leer_bd_mp_sistema()
    items = []
    total_usd = 0.0
    sin_costo = 0
    for r in rows:
        bod = str(r.get("cod_bodega", "")).strip()
        if cod_bod and bod != cod_bod:
            continue
        if buscar and not _coincide_nombre_mp(str(r.get("nombre_mp", "")), buscar):
            continue

        stock = _to_float(r.get("stock_actual", 0), 0.0)
        costo = _to_float(r.get("costo_unitario_ref", 0), 0.0)

        if costo <= 0:
            sin_costo += 1
            if buscar:
                items.append(
                    {
                        "cod_mp_sistema": str(r.get("cod_mp_sistema", "")).strip(),
                        "nombre_mp": str(r.get("nombre_mp", "")).strip(),
                        "cod_bodega": bod,
                        "stock_actual": round(stock, 4),
                        "stock_valorizado": round(stock, 4),
                        "unidad": str(r.get("unidad_base", "")).strip(),
                        "costo_unitario_ref": 0.0,
                        "valor_usd": 0.0,
                        "sin_costo_unitario_ref": True,
                        "nota": "Sin costo_unitario_ref en BD_MP_SISTEMA: no se puede valorizar en USD.",
                    }
                )
            continue

        if (not incluir_cero) and abs(stock) < 1e-9 and not buscar:
            continue

        stock_val = stock if incluir_negativos else max(stock, 0.0)
        val = stock_val * costo
        total_usd += val
        row_item = {
            "cod_mp_sistema": str(r.get("cod_mp_sistema", "")).strip(),
            "nombre_mp": str(r.get("nombre_mp", "")).strip(),
            "cod_bodega": bod,
            "stock_actual": round(stock, 4),
            "stock_valorizado": round(stock_val, 4),
            "unidad": str(r.get("unidad_base", "")).strip(),
            "costo_unitario_ref": round(costo, 6),
            "valor_usd": round(val, 2),
        }
        if buscar and abs(stock) < 1e-9:
            row_item["stock_cero"] = True
        items.append(row_item)

    items.sort(key=lambda x: abs(x["valor_usd"]), reverse=True)
    total_items = len(items)
    if top and top > 0:
        items = items[: _clamp(top, 1, 200)]
    else:
        items = items[offset : offset + limit]

    out = {
        "filtro_nombre_mp": buscar or None,
        "filtro_bodega": cod_bod or None,
        "incluye_negativos": incluir_negativos,
        "total_items_con_costo": total_items,
        "mps_sin_costo_ref": sin_costo,
        "total_valor_usd": round(total_usd, 2),
        "items": items,
        "paging": None if top else {"limit": limit, "offset": offset},
    }
    if buscar and not items:
        out["mensaje"] = (
            "Ninguna fila en BD_MP_SISTEMA coincide con ese texto en nombre_mp "
            "(prueba sin tilde, singular, o parte del nombre como está en la hoja)."
        )
    return out


# ── TOOL 3D — inventario por bodega (resumen) ────────────────
def tool_inventario_por_bodega(args=None):
    """
    Resume stock y valor por bodega desde BD_MP_SISTEMA.
    args opcional:
      - incluir_sin_costo: bool (default False)
    """
    args = args or {}
    incluir_sin_costo = str(args.get("incluir_sin_costo", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}
    incluir_negativos = str(args.get("incluir_negativos", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}

    rows = leer_bd_mp_sistema()
    bodegas: dict[str, dict] = {}
    for r in rows:
        bod = str(r.get("cod_bodega", "")).strip() or "SIN_BODEGA"
        stock = _to_float(r.get("stock_actual", 0), 0.0)
        costo = _to_float(r.get("costo_unitario_ref", 0), 0.0)
        if costo <= 0 and not incluir_sin_costo:
            continue
        stock_val = stock if incluir_negativos else max(stock, 0.0)
        val = stock_val * costo if costo > 0 else 0.0
        b = bodegas.setdefault(
            bod,
            {"cod_bodega": bod, "mps": 0, "valor_usd": 0.0, "stock_total": 0.0, "mps_sin_costo": 0},
        )
        b["mps"] += 1
        b["stock_total"] += stock
        if costo > 0:
            b["valor_usd"] += val
        else:
            b["mps_sin_costo"] += 1

    out = list(bodegas.values())
    for b in out:
        b["valor_usd"] = round(b["valor_usd"], 2)
        b["stock_total"] = round(b["stock_total"], 4)
    out.sort(key=lambda x: x["valor_usd"], reverse=True)
    return {"bodegas": out, "total_bodegas": len(out)}

# ── TOOL 4 — pedidos hoy ────────────────────────────────────
def tool_pedidos_hoy():
    sheet = conectar_sheets()
    hoy = date.today()
    DIA_MAP = {"LUN":0,"MAR":1,"MIE":2,"JUE":3,"VIE":4,"SAB":5,"DOM":6}
    PILOTO = {"ITALDELI","GALABDISTRI","MARAMAR","PACHECO","ELJURI"}
    ws_prov = sheet.worksheet("BD_PROV")
    all_prov = ws_prov.get_all_values()
    headers_prov = None
    proveedores = {}
    for row in all_prov:
        if "cod_proveedor" in row:
            headers_prov = [h.strip() for h in row]; continue
        if headers_prov is None: continue
        r = dict(zip(headers_prov, row))
        cod = str(r.get("cod_proveedor","")).strip()
        razon = str(r.get("razon_social","")).strip().upper()
        if not cod or r.get("proveedor_inventario","").strip().upper() != "SI": continue
        if not any(p in razon for p in PILOTO): continue
        ventana = str(r.get("ventana_pedido","")).strip()
        dias = [d.strip().upper() for d in ventana.split(",")] if ventana else []
        if hoy.weekday() not in [DIA_MAP[d] for d in dias if d in DIA_MAP]: continue
        proveedores[cod] = {
            "nombre": str(r.get("razon_social","")).strip(),
            "lead_time": int(r.get("lead_time_dias",1) or 1),
            "condicion_pago": str(r.get("condicion_pago","")).strip()
        }
    if not proveedores:
        dia = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"][hoy.weekday()]
        return {"pedidos": [], "mensaje": f"Hoy es {dia}, no hay proveedores con ventana de pedido hoy."}
    from inventario_stock_mp import mps_bajo_par

    rows_mp = leer_bd_mp_sistema()
    mps_bajo = {}
    for cod, info in mps_bajo_par(rows_mp).items():
        mps_bajo[cod] = {
            "nombre_mp": info["nombre_mp"],
            "stock": info["stock_total"],
            "par": info["par_level"],
        }
    ws_items = sheet.worksheet("BD_ITEMS_PROV")
    all_items = ws_items.get_all_values()
    headers_items = None
    pedidos = defaultdict(list)
    seen = set()
    for row in all_items:
        if headers_items is None:
            if "cod_mp_sistema" in row: headers_items = [h.strip() for h in row]
            continue
        if str(row[0]).startswith("[FK]"): continue
        r = dict(zip(headers_items, row))
        cod_mp = str(r.get("cod_mp_sistema","")).strip()
        cod_prov = str(r.get("cod_proveedor","")).strip()
        if cod_mp not in mps_bajo or cod_prov not in proveedores: continue
        key = (cod_mp, cod_prov)
        if key in seen: continue
        seen.add(key)
        try: cant_uc = _to_float(r.get("cantidad_unidad_compra", "1") or "1", 1.0)
        except: cant_uc = 1
        falta = mps_bajo[cod_mp]["par"] - mps_bajo[cod_mp]["stock"]
        unidades = math.ceil(falta/cant_uc) if cant_uc > 0 else math.ceil(falta)
        pedidos[cod_prov].append({
            "nombre": mps_bajo[cod_mp]["nombre_mp"],
            "cantidad": unidades,
            "unidad_compra": str(r.get("unidad_compra","")).strip()
        })
    resultado = []
    for cod_prov, items in pedidos.items():
        resultado.append({
            "proveedor": proveedores[cod_prov]["nombre"],
            "condicion_pago": proveedores[cod_prov]["condicion_pago"],
            "items": items, "n_items": len(items)
        })
    return {"fecha": hoy.isoformat(), "pedidos": resultado}

# ── TOOL 5 — top platos semana ──────────────────────────────
def tool_plato_top_semana():
    sb = conectar_supabase()
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,estado_documento",
        [("gte", "fecha", lunes.isoformat()), ("lte", "fecha", hoy.isoformat())])
    rows = _hist_ventas_sin_anulados(rows)
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "semana": f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
        "ranking": [{"posicion": i+1, "plato": n, "unidades": int(c)} for i,(n,c) in enumerate(top)]
    }

# ── TOOL 6 — buscar bodega ──────────────────────────────────
def tool_buscar_bodega(args):
    from bodegas_config import nombre_bodega, normalizar_cod_bodega
    from inventario_stock_mp import norm_mp

    nombre = (args.get("nombre_mp") or "").strip()
    matching = _buscar_mp_por_nombre_o_codigo(nombre)
    if not matching:
        return {"encontrado": False, "mensaje": f"No encontre '{nombre}' en el sistema."}

    opciones = _opciones_mp_desde_hits(matching)
    if len(opciones) > 1:
        lineas = [f"{o['indice']}. {o['texto_usuario']}" for o in opciones]
        return {
            "encontrado": True,
            "requiere_eleccion": True,
            "mensaje": (
                "Varios productos parecidos. Pregunta al usuario cuál necesita:\n"
                + "\n".join(lineas)
            ),
            "opciones": opciones,
        }

    resultados = []
    for r in matching:
        try:
            stock = _to_float(r.get("stock_actual", "0") or "0")
        except Exception:
            stock = 0
        bod = normalizar_cod_bodega(r.get("cod_bodega", ""))
        cod = str(r.get("cod_mp_sistema", "")).strip()
        resultados.append({
            "cod_mp": cod,
            "cod_mp_sistema": norm_mp(cod),
            "nombre_mp": str(r.get("nombre_mp", "")).strip(),
            "cod_bodega": bod,
            "nombre_bodega": nombre_bodega(bod),
            "bodega": bod,
            "stock_actual": round(stock, 2),
            "unidad_base": str(r.get("unidad_base", "")).strip(),
        })
    por_mp: dict[str, float] = {}
    for x in resultados:
        por_mp[x["cod_mp"]] = por_mp.get(x["cod_mp"], 0.0) + x["stock_actual"]
    for x in resultados:
        x["stock_total_mp"] = round(por_mp.get(x["cod_mp"], 0.0), 2)
    return {"encontrado": True, "resultados": resultados}

# ── TOOL 7 — trasladar MP ───────────────────────────────────
def tool_trasladar_mp(args):
    from bodegas_config import (
        nombre_bodega,
        normalizar_cod_bodega,
        resolver_cod_bodega,
        traslado_permitido,
    )
    from inventario_stock_mp import norm_mp

    bodega_origen = resolver_cod_bodega(args.get("bodega_origen", ""))
    bodega_destino = resolver_cod_bodega(args.get("bodega_destino", ""))
    cantidad = float(args.get("cantidad", 0))
    confirmado = args.get("confirmado", False)

    rows = leer_bd_mp_sistema()
    res_mp = _resolver_mp_por_nombre(
        rows,
        nombre_mp=args.get("nombre_mp", ""),
        cod_mp=args.get("cod_mp_sistema", ""),
        bodega_origen=args.get("bodega_origen", ""),
    )
    if not res_mp.get("ok"):
        if res_mp.get("requiere_eleccion"):
            return {
                "requiere_eleccion": True,
                "opciones": res_mp.get("opciones", []),
                "mensaje": res_mp.get("mensaje", ""),
            }
        return {"error": res_mp.get("error", "No se pudo identificar el producto.")}
    cod_mp = res_mp["cod_mp"]
    nombre_mp_resuelto = res_mp.get("nombre_mp", "")

    from unidades_operativas import resolver_cantidad_traslado_mp

    conv = resolver_cantidad_traslado_mp(
        cod_mp,
        cantidad,
        unidad_base="",
        texto=(args.get("texto_original") or args.get("texto") or ""),
        cantidad_presentacion=args.get("cantidad_presentacion"),
        unidad_presentacion=(args.get("unidad_presentacion") or args.get("unidad_pedida") or ""),
    )
    cantidad = float(conv.get("cantidad_base") or 0)
    interpretacion_cant = conv.get("interpretacion") or ""

    if cantidad <= 0:
        return {"error": "La cantidad debe ser mayor que cero."}
    if not traslado_permitido(bodega_origen, bodega_destino):
        return {
            "error": (
                f"Traslado no permitido: {nombre_bodega(bodega_origen) or bodega_origen} → "
                f"{nombre_bodega(bodega_destino) or bodega_destino}. "
                "Válidos: cocina↔barra↔externa; consignación↔barra. "
                "Usa BOD-002/BOD-003 o nombres barra/consignación."
            )
        }

    unidad_base = "UNI"
    nombre_mp = ""
    stock_origen = None
    costo_ref_origen = 0.0
    for r in rows:
        if norm_mp(r.get("cod_mp_sistema")) != cod_mp:
            continue
        if normalizar_cod_bodega(r.get("cod_bodega", "")) == bodega_origen:
            unidad_base = str(r.get("unidad_base", "UNI")).strip()
            nombre_mp = str(r.get("nombre_mp", "")).strip()
            try:
                stock_origen = float(r.get("stock_actual") or 0)
            except (TypeError, ValueError):
                stock_origen = 0.0
            try:
                costo_ref_origen = float(r.get("costo_unitario_ref") or 0)
            except (TypeError, ValueError):
                costo_ref_origen = 0.0
            break

    if stock_origen is None:
        etiqueta = nombre_mp_resuelto or nombre_mp or cod_mp
        return {
            "error": (
                f"{etiqueta} no está registrado en "
                f"{nombre_bodega(bodega_origen)} (origen del traslado)."
                + _hint_fila_maestro_traslado(
                    rows, cod_mp, bodega_origen, rol="origen"
                )
            )
        }

    if cantidad > stock_origen:
        return {
            "error": (
                f"Stock insuficiente en {nombre_bodega(bodega_origen)}: "
                f"disponible {round(stock_origen, 4)} {unidad_base}, "
                f"solicitado {cantidad}."
            )
        }

    fila_destino = next(
        (
            r
            for r in rows
            if norm_mp(r.get("cod_mp_sistema")) == cod_mp
            and normalizar_cod_bodega(r.get("cod_bodega", "")) == bodega_destino
        ),
        None,
    )
    if fila_destino is None:
        return {
            "error": (
                f"No existe fila para {cod_mp} en {nombre_bodega(bodega_destino)}. "
                "Debe crearse primero en BD_MP_SISTEMA."
                + _hint_fila_maestro_traslado(
                    rows, cod_mp, bodega_destino, rol="destino"
                )
            )
        }

    if not confirmado:
        etiqueta = (nombre_mp or nombre_mp_resuelto or cod_mp).strip()
        det_cant = f" ({interpretacion_cant})" if interpretacion_cant else ""
        return {
            "requiere_confirmacion": True,
            "cod_mp_sistema": cod_mp,
            "nombre_mp": etiqueta,
            "stock_origen": round(stock_origen, 4),
            "cantidad_interpretada": round(cantidad, 4),
            "interpretacion_cantidad": interpretacion_cant,
            "mensaje": (
                f"Confirmas trasladar {cantidad:g} {unidad_base} de {etiqueta}{det_cant} "
                f"de {nombre_bodega(bodega_origen)} "
                f"a {nombre_bodega(bodega_destino)}? "
                f"Stock en origen: {round(stock_origen, 4)} {unidad_base}. "
                "Responde 'si confirmo el traslado' para ejecutar."
            ),
        }

    from inventario_traslado import registrar_traslado_mp

    sb = conectar_supabase()
    res = registrar_traslado_mp(
        sb,
        cod_mp=cod_mp,
        bodega_origen=bodega_origen,
        bodega_destino=bodega_destino,
        cantidad=cantidad,
        nombre_mp=nombre_mp,
        unidad_base=unidad_base,
        costo_unitario_ref=costo_ref_origen,
        registrado_por="AGENTE_WHATSAPP",
        recalcular_sheets=True,
        tz=datetime.now(TZ),
    )

    invalidar_cache_bd_mp()
    return {
        "ejecutado": True,
        "cod_mov": res["cod_mov"],
        "mensaje": (
            f"Traslado registrado: {cantidad} {unidad_base} de "
            f"{(nombre_mp or nombre_mp_resuelto or cod_mp).strip()} "
            f"de {nombre_bodega(bodega_origen)} a {nombre_bodega(bodega_destino)}. "
            "Stock y costo ref recalculados en Sheets."
        ),
    }

# ── TOOL 8 — ventas por plato ───────────────────────────────
def _limite_ranking(args: dict) -> int | None:
    """Si args['limite'] es entero > 0, trunca el ranking a ese tamaño; si no, None = sin truncar."""
    raw = args.get("limite")
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def tool_ventas_por_plato(args):
    from ventas_resumen_tools import (
        calcular_resumen_ventas,
        formatear_resumen_ventas_whatsapp,
        resolver_rango_fechas,
    )

    try:
        fecha_ini, fecha_fin, label = resolver_rango_fechas(args)
    except ValueError as e:
        return {"ok": False, "error": str(e), "texto_whatsapp": str(e)}

    sb = conectar_supabase()

    rows = supabase_query_all(
        sb,
        "hist_ventas",
        "nombre_producto,cantidad_vendida,total,estado_documento,cod_smart_menu",
        [("gte", "fecha", fecha_ini), ("lte", "fecha", fecha_fin)],
    )

    orden = (args.get("orden") or "usd").strip().lower()
    if orden not in ("usd", "cantidad"):
        orden = "usd"
    lim = _limite_ranking(args)
    incluir_productos = args.get("incluir_productos")
    if incluir_productos is None:
        incluir_productos = True
    incluir_productos = bool(incluir_productos)
    sin_truncar = args.get("sin_truncar")
    sin_truncar = bool(sin_truncar) if sin_truncar is not None else False

    resumen = calcular_resumen_ventas(rows, orden=orden, limite=lim)
    total_oficial, tickets = _total_desde_hist_ventas_docs(fecha_ini, fecha_fin)
    if total_oficial > 0:
        resumen["total_ventas_usd_oficial"] = total_oficial
        resumen["tickets"] = tickets

    texto = formatear_resumen_ventas_whatsapp(
        resumen,
        periodo_label=label,
        fecha_ini=fecha_ini,
        fecha_fin=fecha_fin,
        incluir_productos=incluir_productos,
        max_items=None if sin_truncar else 50,
    )
    return {
        "periodo": label,
        "fecha_ini": fecha_ini,
        "fecha_fin": fecha_fin,
        **resumen,
        "truncado_a": lim,
        "texto_whatsapp": texto,
    }


def _rango_periodo_ventas(periodo: str) -> tuple[str, str, str]:
    from ventas_resumen_tools import _rango_periodo

    return _rango_periodo(periodo)


# ── TOOL — ventas por día en un rango/mes ────────────────────
def tool_ventas_por_dia(args):
    from ventas_resumen_tools import (
        etiqueta_fecha_ecuador,
        formatear_ventas_por_dia_whatsapp,
        resolver_rango_fechas,
    )

    try:
        fecha_ini, fecha_fin, label = resolver_rango_fechas(args)
    except ValueError as e:
        return {"ok": False, "error": str(e), "texto_whatsapp": str(e)}

    d_ini = date.fromisoformat(fecha_ini)
    d_fin = date.fromisoformat(fecha_fin)
    incluir_dias_cero = bool(args.get("incluir_dias_cero", False))

    por_dia = _totales_por_dia_hist(fecha_ini, fecha_fin)
    dias: list[dict] = []
    total_periodo = 0.0
    tickets_periodo = 0

    d = d_ini
    while d <= d_fin:
        ds = d.isoformat()
        total_dia, tickets_dia = por_dia.get(ds, (0.0, 0))
        total_periodo += total_dia
        tickets_periodo += tickets_dia

        if incluir_dias_cero or total_dia > 0 or tickets_dia > 0:
            meta = etiqueta_fecha_ecuador(ds)
            dias.append(
                {
                    "fecha": ds,
                    "dia_semana": meta["dia_semana"],
                    "total_ventas": total_dia,
                    "tickets": tickets_dia,
                    "fuente": "hist_ventas",
                }
            )
        d += timedelta(days=1)

    fuente = "hist_ventas"

    texto = formatear_ventas_por_dia_whatsapp(
        periodo_label=label,
        fecha_ini=fecha_ini,
        fecha_fin=fecha_fin,
        dias=dias,
        total_periodo=round(total_periodo, 2),
        tickets_periodo=tickets_periodo,
        fuente=fuente,
    )
    return {
        "fecha_ini": fecha_ini,
        "fecha_fin": fecha_fin,
        "periodo": label,
        "dias_con_ventas": len(dias),
        "total_ventas": round(total_periodo, 2),
        "tickets": tickets_periodo,
        "fuente": fuente,
        "dias": dias,
        "texto_whatsapp": texto,
    }

# ── TOOL 9 — rotación baja ──────────────────────────────────
def tool_rotacion_baja(args):
    sb = conectar_supabase()
    hoy = date.today()
    dias = int(args.get("dias", 7))
    umbral = float(args.get("umbral_unidades", 0))
    fecha_ini = (hoy - timedelta(days=dias)).isoformat()

    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,estado_documento",
        [("gte", "fecha", fecha_ini), ("lte", "fecha", hoy.isoformat())])
    rows = _hist_ventas_sin_anulados(rows)

    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0

    baja = {k:v for k,v in conteo.items() if v <= umbral}
    ranking = sorted(baja.items(), key=lambda x: x[1])
    return {
        "periodo_dias": dias,
        "umbral": umbral,
        "total": len(ranking),
        "platos": [{"plato": n, "unidades_vendidas": round(v)} for n,v in ranking[:20]]
    }

# ── TOOL 10 — stock ingrediente ─────────────────────────────
def tool_stock_ingrediente(args):
    from inventario_stock_mp import agrupar_stock_par_por_mp, norm_mp

    nombre = args.get("nombre_mp", "").strip()
    matching = _buscar_mp_por_nombre_o_codigo(nombre)
    if not matching:
        return {"encontrado": False, "mensaje": f"No encontre '{nombre}' en el sistema."}

    opciones = _opciones_mp_desde_hits(matching)
    if len(opciones) > 1:
        lineas = [f"{o['indice']}. {o['texto_usuario']}" for o in opciones]
        return {
            "encontrado": True,
            "requiere_eleccion": True,
            "mensaje": (
                "Varios productos parecidos. Pregunta al usuario cuál quiere consultar:\n"
                + "\n".join(lineas)
            ),
            "opciones": opciones,
        }

    agrupado = agrupar_stock_par_por_mp(matching)
    resultados = []
    for r in matching:
        try:
            stock = _to_float(r.get("stock_actual", "0") or "0")
            par = _to_float(r.get("par_level", "0") or "0")
        except Exception:
            stock = par = 0
        cod = str(r.get("cod_mp_sistema", "")).strip()
        g = agrupado.get(norm_mp(cod), {})
        resultados.append({
            "cod_mp": cod,
            "nombre_mp": str(r.get("nombre_mp", "")).strip(),
            "stock_actual": round(stock, 2),
            "stock_total_mp": g.get("stock_total", round(stock, 2)),
            "par_level": round(par, 2),
            "unidad_base": str(r.get("unidad_base", "")).strip(),
            "bodega": str(r.get("cod_bodega", "")).strip(),
            "bajo_par": stock < par,
            "bajo_par_global": g.get("bajo_par", stock < par),
        })
    return {"encontrado": True, "resultados": resultados}


def tool_consumo_ingrediente_recetas(args):
    """
    Consumo teórico de MP según ventas (PROCESADO) y BD_RECETAS_DETALLE.
    Usa funciones migradas en este módulo (sin consultas_chat_extendidas).
    """
    nombre_mp = (args.get("nombre_mp") or "").strip()
    if not nombre_mp:
        return {"error": "Falta nombre_mp (nombre o cod_mp_sistema en BD_MP_SISTEMA)."}

    fecha_ini = (args.get("fecha_ini") or "").strip()
    fecha_fin = (args.get("fecha_fin") or "").strip()
    periodo = (args.get("periodo") or "semana").strip().lower()

    hoy = date.today()
    if fecha_ini and fecha_fin:
        fi, ff = fecha_ini, fecha_fin
    elif periodo in ("hoy", "dia", "dia_actual"):
        fi = ff = hoy.isoformat()
    elif periodo in ("mes", "mes_actual"):
        fi = date(hoy.year, hoy.month, 1).isoformat()
        ff = hoy.isoformat()
    else:
        lunes = hoy - timedelta(days=hoy.weekday())
        fi, ff = lunes.isoformat(), hoy.isoformat()

    hits = _buscar_mp_por_nombre_o_codigo(nombre_mp)
    if not hits:
        return {"error": f"No encontre '{nombre_mp}' en BD_MP_SISTEMA."}

    if len(hits) > 1:
        return {
            "ambiguo": True,
            "opciones": [
                {
                    "cod_mp_sistema": (h.get("cod_mp_sistema") or "").strip(),
                    "nombre_mp": (h.get("nombre_mp") or "").strip(),
                }
                for h in hits[:15]
            ],
            "mensaje": "Varias MPs coinciden; pide cod_mp exacto o nombre mas especifico.",
        }

    cod = (hits[0].get("cod_mp_sistema") or "").strip()
    nom = (hits[0].get("nombre_mp") or "").strip()
    unidad = (hits[0].get("unidad_base") or "").strip()

    out = calcular_consumo_teorico_mp(fi, ff, cod)
    if not isinstance(out, dict):
        return {"error": "Respuesta invalida del calculo."}
    out["nombre_mp_resuelto"] = nom
    out["unidad_base"] = unidad
    out["periodo_solicitado"] = {"fecha_ini": fi, "fecha_fin": ff}
    return out


# ── Costo teórico platos (BD_RECETAS_DETALLE + MPs + subrecetas) ─
def _buscar_platos_receta(
    *,
    cod_receta: str = "",
    nombre_plato: str = "",
    variedad: str = "",
) -> list[tuple[str, list[dict]]]:
    from calcular_costo_recetas import cargar_contexto_costos
    from recetas_detalle import clave_plato, norm_cod_receta

    _, _, por_plato, _ = cargar_contexto_costos()
    cod = (cod_receta or "").strip()
    nom_q = (nombre_plato or "").strip().lower()
    var = (variedad or "").strip()

    if cod:
        nk = norm_cod_receta(cod)
        if var:
            key = clave_plato(cod, var)
            if key in por_plato:
                return [(key, por_plato[key])]
            return []
        out: list[tuple[str, list[dict]]] = []
        for key, lineas in por_plato.items():
            if lineas and norm_cod_receta(lineas[0].get("cod_receta") or "") == nk:
                out.append((key, lineas))
        return out

    if nom_q:
        hits: list[tuple[str, list[dict]]] = []
        for key, lineas in por_plato.items():
            if not lineas:
                continue
            nombre = (lineas[0].get("nombre_receta") or "").strip().lower()
            varied = (lineas[0].get("variedad_smart_menu") or "").strip().lower()
            if nom_q in nombre or nom_q in varied:
                if var and var.lower() not in varied:
                    continue
                hits.append((key, lineas))
        return hits

    return []


def tool_costo_plato(args):
    """
    Costo teórico por 1 plato vendido (MP + subrecetas), con desglose por línea.
    """
    from calcular_costo_recetas import cargar_contexto_costos, resumen_plato_costo

    cod = (args.get("cod_receta") or "").strip()
    nombre = (args.get("nombre_plato") or args.get("nombre_receta") or "").strip()
    variedad = (args.get("variedad_smart_menu") or args.get("variedad") or "").strip()
    ocultar_costos = bool(args.get("_ocultar_costos"))
    incluir_ingredientes = args.get("incluir_ingredientes")
    # Default: SOLO costo (evita spam). Si quieren ingredientes, lo piden explícitamente.
    if incluir_ingredientes is None:
        incluir_ingredientes = False
    incluir_ingredientes = bool(incluir_ingredientes)

    if not cod and not nombre:
        return {"error": "Indica cod_receta o nombre_plato (nombre en BD_RECETAS_DETALLE)."}

    matches = _buscar_platos_receta(
        cod_receta=cod, nombre_plato=nombre, variedad=variedad
    )
    if not matches:
        return {
            "encontrado": False,
            "mensaje": "No encontre plato en BD_RECETAS_DETALLE con esos criterios.",
        }
    if len(matches) > 1 and not variedad and not (
        cod and len(matches) == 1
    ):
        opciones = []
        for key, lineas in matches[:20]:
            ln0 = lineas[0]
            opciones.append(
                {
                    "clave": key,
                    "cod_receta": (ln0.get("cod_receta") or "").strip(),
                    "variedad_smart_menu": (ln0.get("variedad_smart_menu") or "").strip(),
                    "nombre_receta": (ln0.get("nombre_receta") or "").strip(),
                }
            )
        return {
            "ambiguo": True,
            "opciones": opciones,
            "mensaje": "Varias variedades o coincidencias; repite con cod_receta y variedad_smart_menu.",
        }

    costos_mp, unitarios_sub, _, _ = cargar_contexto_costos()
    _key, lineas = matches[0]
    res = resumen_plato_costo(lineas, costos_mp, unitarios_sub)
    detalle = sorted(
        res.get("detalle_lineas") or [],
        key=lambda x: x.get("costo_linea", 0),
        reverse=True,
    )
    costo = float(res.get("costo_plato_estandar") or 0.0)
    nombre_out = (res.get("nombre_receta") or "").strip() or "(sin nombre)"
    var_out = (res.get("variedad_smart_menu") or "").strip()
    titulo = nombre_out + (f" ({var_out})" if var_out else "")

    if ocultar_costos:
        top_n = int(args.get("top") or 0) if str(args.get("top") or "").strip() else 0
        top_n = top_n if top_n > 0 else 40
        lines = [f"Ingredientes de {titulo} (por 1 unidad):", ""]
        for d in detalle[:top_n]:
            nom_i = (d.get("nombre") or "").strip()
            cant = d.get("cantidad")
            ub = (d.get("unidad_base") or "").strip()
            if nom_i:
                lines.append(f"- {nom_i} {cant}{ub}")
        if len(detalle) > top_n:
            lines.append(f"... y {len(detalle) - top_n} ingrediente(s) más.")
        texto = "\n".join(lines)
        return {
            "encontrado": True,
            "cod_receta": res.get("cod_receta"),
            "nombre_receta": res.get("nombre_receta"),
            "sin_costos": True,
            "texto_whatsapp": texto,
        }

    if not incluir_ingredientes:
        texto = (
            f"El costo de preparar un {titulo} es de {costo:.2f} USD "
            f"(costo teórico estándar).\n\n"
            "¿Quieres ver el desglose de ingredientes? (responde: 'ingredientes' o 'detalle')"
        )
    else:
        top_n = int(args.get("top") or 0) if str(args.get("top") or "").strip() else 0
        top_n = top_n if top_n > 0 else 25
        lines = [
            f"El costo de preparar un {titulo} es de {costo:.2f} USD (costo teórico estándar).",
            "",
            "Desglose de ingredientes principales:",
        ]
        for d in detalle[:top_n]:
            nom_i = (d.get("nombre") or "").strip()
            cant = d.get("cantidad")
            ub = (d.get("unidad_base") or "").strip()
            cl = float(d.get("costo_linea") or 0.0)
            if nom_i:
                lines.append(f"- {nom_i} {cant}{ub} - {cl:.2f} USD")
        if len(detalle) > top_n:
            lines.append(f"... y {len(detalle) - top_n} ingrediente(s) más (pide 'todos' si lo necesitas).")
        lines.append("")
        lines.append(f"Total: {costo:.2f} USD por plato")
        texto = "\n".join(lines)

    return {
        "encontrado": True,
        "cod_receta": res.get("cod_receta"),
        "variedad_smart_menu": res.get("variedad_smart_menu"),
        "nombre_receta": res.get("nombre_receta"),
        "costo_plato_estandar_usd": res.get("costo_plato_estandar"),
        "n_lineas_mp": res.get("n_lineas_mp"),
        "n_lineas_sub": res.get("n_lineas_sub"),
        "lineas_sin_costo": res.get("lineas_sin_costo"),
        "notas": res.get("notas_costo"),
        "desglose": detalle,
        "texto_whatsapp": texto,
        "nota": "Costo por 1 unidad vendida; subrecetas recalculadas desde MPs. Recalcular: calcular_costo_recetas.py --produccion",
    }


def _norm_sub_cod_wa(cod: str) -> str:
    from codigos_subreceta import cod_sub_canonico

    return cod_sub_canonico(cod)


def _buscar_subrecetas(
    *,
    cod_subreceta: str = "",
    nombre_subreceta: str = "",
) -> list[tuple[str, dict, list[dict]]]:
    from calcular_costo_subrecetas import cargar_contexto_subrecetas

    cab, por_padre, _, _ = cargar_contexto_subrecetas()
    cod = (cod_subreceta or "").strip()
    nom_q = (nombre_subreceta or "").strip().lower()
    hits: list[tuple[str, dict, list[dict]]] = []

    if cod:
        nk = _norm_sub_cod_wa(cod)
        for c, info in cab.items():
            if _norm_sub_cod_wa(c) == nk:
                hits.append((c, info, por_padre.get(c, [])))
        return hits

    if nom_q:
        for c, info in cab.items():
            nombre = (info.get("nombre_subreceta") or "").strip().lower()
            if nom_q in nombre:
                hits.append((c, info, por_padre.get(c, [])))
        return hits

    return []


def _es_palabra_subreceta_sola(texto: str) -> bool:
    """Solo la palabra subreceta(s), sin verbo de producción ni otra intención."""
    t = re.sub(r"\s+", " ", (texto or "").strip().lower())
    return t in ("subreceta", "subrecetas", "sub receta", "sub recetas")


def _fecha_consulta_ventas_simple(texto: str) -> str:
    """Fecha ISO para consulta ventas del día (hoy por defecto)."""
    t = re.sub(r"\s+", " ", (texto or "").strip().lower())
    if re.search(r"\bayer\b", t):
        return (_fecha_hoy_ec() - timedelta(days=1)).isoformat()
    return _fecha_hoy_ec().isoformat()


def _es_consulta_ventas_simple(texto: str) -> bool:
    """Ventas de hoy/ayer o total del día — sin LLM."""
    t = re.sub(r"\s+", " ", (texto or "").strip().lower())
    t = t.replace("ó", "o").replace("í", "i").replace("ú", "u").replace("é", "e").replace("á", "a")
    if not t:
        return False
    tiene_ventas = bool(
        re.search(
            r"\b(ventas?|vendimos|vendio|vendido|facturacion|facturado|facturo)\b",
            t,
        )
        or re.search(r"\bse vend", t)
        or re.search(r"\bvendid", t)
    )
    if not tiene_ventas:
        return False
    if re.search(
        r"\b(por plato|por dia|por dia|ranking|productos|platos|semana|mes|"
        r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|"
        r"octubre|noviembre|diciembre|desglose)\b",
        t,
    ):
        return False
    if re.search(r"\b(hoy|ayer|del dia|de hoy|de ayer)\b", t) and len(t.split()) <= 12:
        return True
    if t in (
        "ventas",
        "venta",
        "las ventas",
        "ventas hoy",
        "hoy ventas",
        "vendimos",
        "vendimos hoy",
        "cuanto vendimos",
        "cuanto vendimos hoy",
        "dame las ventas",
        "dame las ventas de hoy",
        "dame ventas",
        "dime las ventas",
        "ventas de hoy",
        "cuanto se vendio",
        "cuanto se vendio hoy",
        "que se vendio hoy",
    ):
        return True
    return bool(
        re.search(r"\b(cuanto|total|dame|dime|muestrame|quiero saber)\b", t)
        and len(t.split()) <= 8
    )


def _es_consulta_lista_subrecetas(texto: str) -> bool:
    """Pregunta informativa (catálogo), no comando de producción."""
    t = re.sub(r"\s+", " ", (texto or "").strip().lower())
    if not re.search(r"\bsub[- ]?recetas?\b", t):
        return False
    if re.search(r"\b(producir|preparar|registrar|confirmar|simular)\b", t):
        return False
    if re.search(r"\bproducci", t):
        return False
    if re.search(r"\b(costo|precio|ingredientes?|receta de|detalle de)\b", t):
        if not re.search(
            r"\b(lista|listado|catalogo|catálogo|todas?|completa|cuales|cuáles|hay|existen)\b",
            t,
        ):
            return False
    return bool(
        re.search(r"\b(lista|listado|catalogo|catálogo)\b", t)
        or re.search(r"\b(cuales|cuáles|que|qué)\s+.*\bsub", t)
        or re.search(r"\bsubrecetas?\b.*\b(hay|existen|tenemos|disponibles?|todas?)\b", t)
        or re.search(r"\b(listar|enumera|muestra|dame|entrega)\b.*\bsub", t)
        or re.search(r"\bsubrecetas?\b.*\b(completa|completo)\b", t)
        or re.search(r"\btodas?\s+las?\s+subrecetas?\b", t)
    )


def _texto_lista_subrecetas_whatsapp(area: str | None = None) -> str:
    """Catálogo completo de subrecetas activas en BD_SUBRECETAS."""
    from codigos_subreceta import cod_sub_sin_prefijo
    from subrecetas_bodegas_stock import SUBRECETAS_BARRA
    from subrecetas_detalle import cargar_bd_subrecetas

    cab = cargar_bd_subrecetas(conectar_sheets())
    barra: list[tuple[str, dict]] = []
    cocina: list[tuple[str, dict]] = []
    for cod, info in cab.items():
        if (info.get("activa") or "SI").strip().upper() == "NO":
            continue
        if cod in SUBRECETAS_BARRA:
            barra.append((cod, info))
        else:
            cocina.append((cod, info))
    barra.sort(key=lambda x: cod_sub_sin_prefijo(x[0]))
    cocina.sort(key=lambda x: cod_sub_sin_prefijo(x[0]))

    def _linea(cod: str, info: dict) -> str:
        num = cod_sub_sin_prefijo(cod)
        nom = (info.get("nombre_subreceta") or "").strip()
        rend = (info.get("rendimiento_estandar") or "").strip()
        un = (info.get("unidad") or info.get("unidad_base") or "").strip()
        if rend and un:
            return f"• {num} {nom} — {rend} {un}"
        if rend:
            return f"• {num} {nom} — {rend}"
        return f"• {num} {nom}"

    parts: list[str] = []
    if area in (None, "barra") and barra:
        lines = [_linea(c, i) for c, i in barra]
        parts.append(f"*Barra* (BOD-002) — {len(lines)} batches\n" + "\n".join(lines))
    if area in (None, "cocina") and cocina:
        lines = [_linea(c, i) for c, i in cocina]
        parts.append(
            f"*Cocina* (BOD-001 / BOD-005) — {len(lines)} subrecetas\n" + "\n".join(lines)
        )
    if not parts:
        return "No hay subrecetas activas en BD_SUBRECETAS."
    if area == "barra":
        n = len(barra)
    elif area == "cocina":
        n = len(cocina)
    else:
        n = len(barra) + len(cocina)
    intro = f"Catálogo de subrecetas activas ({n}):\n\n"
    footer = (
        "\n\nPara producir: PRODUCIR SUB <código> BOD-00x\n"
        "Para costo de una sub: pregunta por nombre o código."
    )
    return intro + "\n\n".join(parts) + footer


def tool_listar_subrecetas(args):
    area = (args.get("area") or "").strip().lower() or None
    if area not in ("barra", "cocina"):
        area = _parse_area_produccion((args.get("texto") or "")) or area
    if area not in ("barra", "cocina"):
        area = None
    texto = _texto_lista_subrecetas_whatsapp(area=area)
    from subrecetas_bodegas_stock import SUBRECETAS_BARRA
    from subrecetas_detalle import cargar_bd_subrecetas

    cab = cargar_bd_subrecetas(conectar_sheets())
    activas = [
        c
        for c, i in cab.items()
        if (i.get("activa") or "SI").strip().upper() != "NO"
    ]
    return {
        "area": area or "todas",
        "total_activas": len(activas),
        "total_barra": sum(1 for c in activas if c in SUBRECETAS_BARRA),
        "total_cocina": sum(1 for c in activas if c not in SUBRECETAS_BARRA),
        "texto_whatsapp": texto,
        "nota": "Copia texto_whatsapp tal cual; no omitas filas.",
    }


def tool_costo_subreceta(args):
    """Costo teórico del lote estándar de una subreceta (MPs + hijos) con desglose."""
    from calcular_costo_subrecetas import (
        calcular_costos,
        cargar_contexto_subrecetas,
        resumen_subreceta_costo,
    )

    cod = (args.get("cod_subreceta") or "").strip()
    nombre = (args.get("nombre_subreceta") or "").strip()

    if not cod and not nombre:
        return {"error": "Indica cod_subreceta o nombre_subreceta."}

    matches = _buscar_subrecetas(cod_subreceta=cod, nombre_subreceta=nombre)
    if not matches:
        return {
            "encontrado": False,
            "mensaje": "No encontre subreceta en BD_SUBRECETAS con esos criterios.",
        }
    if len(matches) > 1 and not cod:
        opciones = [
            {
                "cod_subreceta": c,
                "nombre_subreceta": (info.get("nombre_subreceta") or "").strip(),
                "rendimiento_estandar": info.get("rendimiento_estandar"),
                "unidad": (info.get("unidad") or "").strip(),
            }
            for c, info, _ in matches[:20]
        ]
        return {
            "ambiguo": True,
            "opciones": opciones,
            "mensaje": "Varias subrecetas coinciden; repite con cod_subreceta.",
        }

    cab_all, por_padre, costos_mp, _ = cargar_contexto_subrecetas()
    resultados, _ = calcular_costos(cab_all, por_padre, costos_mp)
    cod_key, cab, lineas = matches[0]
    res = resumen_subreceta_costo(cod_key, cab, lineas, costos_mp, resultados)
    res["encontrado"] = True
    res["nota"] = (
        "Cantidades del lote estandar (rendimiento_estandar). "
        "Costo unitario = costo_lote / rendimiento. "
        "Recalcular: calcular_costo_subrecetas.py --produccion"
    )
    return res


def tool_receta_ingredientes(args):
    """Alias orientado a cantidades + costos por línea de un plato (misma fuente que costo_plato)."""
    out = tool_costo_plato(args)
    if out.get("encontrado"):
        out["tipo_consulta"] = "receta_plato"
        out["ingredientes"] = out.pop("desglose", [])
    return out


def tool_auditar_costos_recetas(args=None):
    """Resumen de platos con costo inflado y MPs sospechosos en recetas."""
    from auditar_costos_recetas import auditar

    args = args or {}
    umbral_plato = _to_float(args.get("umbral_plato"), 25.0)
    umbral_linea = _to_float(args.get("umbral_linea"), 20.0)
    top_p = int(args.get("top_platos") or 10)
    top_l = int(args.get("top_lineas") or 12)

    platos, lineas = auditar(
        umbral_plato=umbral_plato,
        umbral_linea=umbral_linea,
        umbral_cu_gr=_to_float(args.get("umbral_cu_gr"), 0.08),
    )
    return {
        "platos_inflados_total": len(platos),
        "lineas_mp_sospechosas_total": len(lineas),
        "umbral_plato_usd": umbral_plato,
        "umbral_linea_usd": umbral_linea,
        "platos_inflados": platos[:top_p],
        "lineas_mp_sospechosas": lineas[:top_l],
        "nota": (
            "Flags comunes: linea_mp_cara, costo_unitario_alto_gr_ml, "
            "posible_precio_kg_como_gr_x1000. CSV completo: auditar_costos_recetas.py"
        ),
    }


# ── TOOL — ventas día específico ─────────────────────────
def tool_ventas_dia(args):
    from ventas_resumen_tools import etiqueta_fecha_ecuador, formatear_ventas_dia_whatsapp

    fecha = args.get("fecha", "").strip()
    if not fecha:
        fecha = date.today().isoformat()

    incluir_productos = args.get("incluir_productos")
    if incluir_productos is None:
        incluir_productos = False
    incluir_productos = bool(incluir_productos)

    meta_fecha = etiqueta_fecha_ecuador(fecha)
    sm = total_smartmenu_dia(fecha)

    sb = conectar_supabase()
    rows = supabase_query_all(
        sb,
        "hist_ventas",
        "nombre_producto,cantidad_vendida,total,estado_documento,num_documento",
        [("eq", "fecha", fecha)],
    )
    rows = _hist_ventas_sin_anulados(rows)

    if not rows and sm is None:
        texto = formatear_ventas_dia_whatsapp(
            etiqueta_fecha=meta_fecha["etiqueta_fecha"],
            total_ventas=0,
            tickets=0,
            fuente="hist_ventas",
            platos=[],
            incluir_productos=False,
        )
        return {
            "fecha": fecha,
            **meta_fecha,
            "total_ventas": 0,
            "tickets": 0,
            "platos": [],
            "sin_datos": True,
            "incluir_productos": False,
            "texto_whatsapp": texto,
        }

    conteo = defaultdict(lambda: {"cantidad": 0, "total": 0})
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(sin nombre)"
        conteo[nombre]["cantidad"] += _to_float(r.get("cantidad_vendida"), 0)
        conteo[nombre]["total"] += _to_float(r.get("total"), 0.0)
    # Ordenar por monto (USD neto) para que el "que se vendió" sea un ranking útil.
    ranking_full = sorted(conteo.items(), key=lambda x: x[1]["total"], reverse=True)
    lim = _limite_ranking(args)
    ranking = ranking_full if lim is None else ranking_full[:lim]

    platos = [
        {"plato": n, "cantidad": round(d["cantidad"]), "total_usd": round(d["total"], 2)}
        for n, d in ranking
    ]

    resultado = {
        "fecha": fecha,
        **meta_fecha,
        "incluir_productos": incluir_productos,
        "total_productos_distintos": len(conteo),
        "truncado_a": lim,
    }
    if incluir_productos:
        resultado["platos"] = platos
    if _smartmenu_dia_valido(sm):
        resultado["total_ventas"] = round(sm.get("total", 0), 2)
        resultado["ventas_brutas"] = round(sm.get("total_bruto", sm.get("total", 0)), 2)
        resultado["descuentos"] = round(sm.get("total_descuentos", 0), 2)
        resultado["tickets"] = sm.get("docs", 0)
        resultado["fuente"] = "Smart Menu"
    else:
        total_hv, docs_hv = _total_desde_hist_ventas_docs(fecha, fecha)
        resultado["total_ventas"] = total_hv
        resultado["tickets"] = docs_hv
        resultado["fuente"] = "hist_ventas"
    resultado["texto_whatsapp"] = formatear_ventas_dia_whatsapp(
        etiqueta_fecha=meta_fecha["etiqueta_fecha"],
        total_ventas=resultado.get("total_ventas", 0),
        tickets=resultado.get("tickets", 0),
        fuente=resultado.get("fuente", ""),
        platos=platos,
        incluir_productos=incluir_productos,
        total_productos_distintos=len(conteo),
    )
    return resultado


# ── TOOLS conteo físico (inicio de flujo vía WA) ───────────────
def tool_conteo_iniciar(args):
    cod_bodega = (args.get("cod_bodega") or "").strip()
    anio = args.get("anio")
    semana = args.get("semana_iso")
    try:
        return iniciar_conteo_wa(
            cod_bodega,
            anio=int(anio) if anio is not None else None,
            semana_iso=int(semana) if semana is not None else None,
            sheet_name=(args.get("sheet_name") or "").strip() or None,
            reemplazar_snapshot=bool(args.get("reemplazar_snapshot")),
            sobreescribir_hoja=bool(args.get("sobreescribir_hoja", True)),
            responsable_nombre=(args.get("responsable_nombre") or "").strip() or None,
            responsable_contacto=(args.get("responsable_contacto") or "").strip() or None,
            notas=(args.get("notas") or "").strip() or None,
        )
    except ConteoOperacionError as e:
        return {"ok": False, "error": e.code, "mensaje": e.message, "detalles": e.details}
    except Exception as e:
        return {"ok": False, "error": "ERROR", "mensaje": str(e)}


def tool_conteo_listar_ciclos(args):
    from conteo_fisico import listar_ciclos_api

    estado = (args.get("estado") or "").strip() or None
    cod_bodega = (args.get("cod_bodega") or "").strip() or None
    limit = int(args.get("limit") or 10)
    rows = listar_ciclos_api(estado=estado, cod_bodega=cod_bodega, limit=limit)
    return {
        "total": len(rows),
        "ciclos": [
            {
                "ciclo_id": r.get("id"),
                "estado": r.get("estado"),
                "cod_bodega": r.get("cod_bodega"),
                "anio": r.get("anio"),
                "semana_iso": r.get("semana_iso"),
                "sheet_name": r.get("sheet_name"),
                "snapshot_at": r.get("snapshot_at"),
            }
            for r in rows
        ],
    }


def tool_conteo_ciclos_abiertos(args=None):
    return resumen_ciclos_abiertos()


def tool_produccion_subreceta(args):
    cods_raw = args.get("cod_subreceta") or args.get("codigos") or []
    if isinstance(cods_raw, str):
        cods = [c.strip() for c in cods_raw.replace(",", " ").split() if c.strip()]
    else:
        cods = [str(c).strip() for c in cods_raw if str(c).strip()]
    if not cods and args.get("cod_subreceta"):
        cods = [str(args.get("cod_subreceta")).strip()]
    wa = (args.get("_wa_id") or args.get("registrado_por") or "").strip()
    from estrategia_config import bodega_default_produccion_sub, validar_bodega_produccion_sub

    bodega = (args.get("bodega") or "").strip().upper()
    if not bodega and wa:
        bodega = bodega_default_produccion_sub(wa)
    if not bodega:
        bodega = "BOD-002"
    if wa:
        err = validar_bodega_produccion_sub(wa, bodega)
        if err:
            return {"ok": False, "error": "BODEGA", "mensaje": err}
    from unidades_operativas import resolver_cantidad_produccion_sub

    texto_orig = (args.get("texto_original") or args.get("texto") or "").strip()
    cant_raw = float(args["cantidad"]) if args.get("cantidad") is not None else None
    cant_lotes = args.get("cantidad_lotes")
    if cant_lotes is not None:
        try:
            cant_lotes = float(cant_lotes)
        except (TypeError, ValueError):
            cant_lotes = None
    cantidad_resuelta = None
    interpretaciones: list[str] = []
    if cods:
        conv = resolver_cantidad_produccion_sub(
            cods[0],
            cant_raw,
            texto=texto_orig,
            cantidad_lotes=cant_lotes,
        )
        if conv.get("cantidad_base") is not None:
            cantidad_resuelta = float(conv["cantidad_base"])
        if conv.get("interpretacion"):
            interpretaciones.append(str(conv["interpretacion"]))
    try:
        out = producir_subreceta_wa(
            cods,
            bodega=bodega,
            cantidad=cantidad_resuelta if cantidad_resuelta is not None else cant_raw,
            registrado_por=(args.get("registrado_por") or "WhatsApp").strip(),
            simular=bool(args.get("simular", True)),
            forzar=bool(args.get("forzar")),
            recalcular=bool(args.get("recalcular", True)),
        )
        if interpretaciones and isinstance(out, dict):
            tw = (out.get("texto_whatsapp") or "").strip()
            nota = "Cantidad: " + "; ".join(interpretaciones)
            out["texto_whatsapp"] = (nota + "\n\n" + tw) if tw else nota
        return out
    except SubrecetaOperacionError as e:
        return {"ok": False, "error": e.code, "mensaje": e.message}
    except Exception as e:
        return {"ok": False, "error": "ERROR", "mensaje": str(e)}


_sub_alias_cache: list[tuple[str, str]] | None = None
_sub_alias_cache_at: float = 0.0
_SUB_ALIAS_TTL_SEC = 300

_BATCH_ALIASES: list[tuple[tuple[str, ...], str]] = [
    (("ron banana", "banana negroni", "run banana", "ron ban"), "053"),
    (("tokio mule", "tokio"), "052"),
    (("mojito de coco", "mojito coco", "coconut mojito", "coco mojito"), "054"),
    (("classic negroni", "batch negroni", "negroni"), "051"),
]

_COCINA_ALIASES: list[tuple[tuple[str, ...], str]] = [
    (("pan bao",), "006"),
    (("salsa ponzu", "ponzu"), "016"),
    (("mayonesa ponzu",), "017"),
    (("salsa gochuyan", "gochujang"), "009"),
    (("kimchi caramelizado",), "037"),
    (("kimchi",), "036"),
    (("salsa de miso", "salsa miso"), "002"),
    (("salsa char siu", "char siu"), "055"),
    (("costillas char siu",), "056"),
    (("mayonesa siracha", "sriracha"), "015"),
    (("ensalada de col", "cole slaw"), "012"),
    (("cebolla curtida", "cebollas curtidas"), "018"),
    (("huevos ajitama", "ajitama"), "040"),
    (("salsa agridulce",), "039"),
    (("salsa tonkatsu",), "042"),
    (("salsa oriental",), "058"),
    (("salsa drunken",), "059"),
    (("aceite jengibre", "aceite de jengibre"), "044"),
    (("torta de chocolate", "tortas de chocolate", "torta chocolate", "tortas de choclate", "torta choclate"), "061"),
]


def _aliases_subrecetas() -> list[tuple[str, str]]:
    """(frase en minúsculas, código 3 dígitos) — frases largas primero."""
    global _sub_alias_cache, _sub_alias_cache_at
    now = time.monotonic()
    if _sub_alias_cache and (now - _sub_alias_cache_at) < _SUB_ALIAS_TTL_SEC:
        return _sub_alias_cache
    pairs: list[tuple[str, str]] = []
    for groups, cod in _BATCH_ALIASES + _COCINA_ALIASES:
        for g in groups:
            pairs.append((g.lower(), cod))
    try:
        from codigos_subreceta import cod_sub_canonico
        from subrecetas_detalle import cargar_bd_subrecetas

        cab = cargar_bd_subrecetas(conectar_sheets())
        for cod_raw, info in cab.items():
            if (info.get("activa") or "SI").strip().upper() == "NO":
                continue
            cod = cod_sub_canonico(cod_raw).replace("SUB-", "").zfill(3)
            nom = (info.get("nombre_subreceta") or "").strip().lower()
            if len(nom) >= 3:
                pairs.append((nom, cod))
    except Exception as e:
        print(f"WARN aliases subrecetas: {e}")
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for phrase, cod in sorted(pairs, key=lambda x: (-len(x[0]), x[0])):
        key = f"{phrase}|{cod}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append((phrase, cod))
    _sub_alias_cache = uniq
    _sub_alias_cache_at = now
    return uniq


def _match_sub_codigos_en_texto(texto: str) -> list[str]:
    t_clean = _texto_sin_cantidad_sub((texto or "").lower())
    hits: list[tuple[int, str]] = []
    for phrase, cod in _aliases_subrecetas():
        if phrase in t_clean:
            hits.append((len(phrase) + 100, cod))
    if not hits:
        for phrase, cod in _aliases_subrecetas():
            if _coincide_nombre_sub(phrase, t_clean):
                hits.append((len(_tokens_sub_nombre(phrase)) * 10 + len(phrase), cod))
    if not hits and not _es_contexto_bodegas_no_sub(texto):
        for m in re.finditer(r"(?:sub[- ]?)?(0\d{2})\b", t_clean, re.I):
            hits.append((3, m.group(1).zfill(3)))
    if not hits:
        return []
    hits.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _, cod in hits:
        if cod not in seen:
            seen.add(cod)
            out.append(cod)
    return out


def _parse_producir_sub_comando(texto: str, wa_id: str | None = None) -> dict | None:
    """PRODUCIR SUB 006 [500 GR] BOD-001 CONFIRMAR → plan o registro."""
    from estrategia_config import bodega_default_produccion_sub

    raw = (texto or "").strip()
    upper = raw.upper()
    prefix = None
    for p in ("PRODUCIR SUB", "PREPARAR SUB", "PRODUCCION SUB"):
        if upper.startswith(p):
            prefix = p
            break
    if not prefix:
        return None
    resto = raw[len(prefix) :].strip()
    confirmar = "CONFIRMAR" in upper
    limpio = resto.upper().replace("CONFIRMAR", " ").strip()
    tokens = [t for t in limpio.split() if t]
    bodega = bodega_default_produccion_sub(wa_id) if wa_id else "BOD-002"
    area = _resolver_area_produccion(wa_id, resto, cods=[])
    if area:
        bodega = _bodega_por_area(area) or bodega
    cods: list[str] = []
    cantidad: float | None = None
    _units = {"ML", "GR", "G", "L", "LT", "LITRO", "LITROS", "UNI", "UNIDAD", "UNIDADES", "UND"}
    for tok in tokens:
        tu = tok.upper()
        if tu.startswith("BOD-"):
            bodega = tu
            continue
        if tu in _units:
            continue
        num = tok.replace(".", "").replace(",", "")
        if not num.isdigit():
            continue
        val = float(tok.replace(",", "."))
        if len(num) >= 4 or val >= 100:
            cantidad = val
        elif not cods:
            cods.append(num.zfill(3))
        elif cantidad is None and val < 100:
            cods.append(num.zfill(3))
        else:
            cantidad = val
    out: dict = {"cods": cods, "bodega": bodega, "confirmar": confirmar}
    if cantidad is not None:
        out["cantidad"] = cantidad
    return out


_TRASLADO_VERBS_RE = re.compile(
    r"\b(traslad|transfer|transfi|muev|mové|mueve|mover|pasar|pasa|pase)\w*",
    re.I,
)
_TRASLADO_VERBO_INICIO_RE = re.compile(
    r"^(?:traslad\w*|transfer\w*|transfi\w*|muev\w*|mové|mueve|mover|pasar?|pasa|pase)\s+",
    re.I,
)
_BODEGA_TOKEN = (
    r"(?:bod[- ]?)?0?\d{3}|cocina|barra|consignacion|consignación|externa|bodega\s+externa"
)
_TRASLADO_DE_A_RE = re.compile(
    rf"\b(?:de|desde)\s+({_BODEGA_TOKEN})\b.*?\b(?:a|hacia|para)\s+({_BODEGA_TOKEN})\b",
    re.I,
)


def _normalizar_texto_comando_wa(texto: str) -> str:
    """Corrige typos y caracteres invisibles de WhatsApp antes de enrutar."""
    t = (texto or "").strip()
    if not t:
        return t
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff\u00ad]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\braslad", "traslad", t, flags=re.I)
    t = re.sub(r"\btralsad", "traslad", t, flags=re.I)
    t = re.sub(r"\bchoclate\b", "chocolate", t, flags=re.I)
    return t


def _fragmento_insumo_traslado(texto: str) -> str:
    """Texto del ítem antes del tramo «de X a Y» (sin verbos de traslado)."""
    return _texto_item_traslado(_normalizar_texto_comando_wa(texto))


def _es_traslado_implicito(texto: str) -> bool:
    """«5 tortas de chocolate de 005 a 001» sin verbo traslado explícito."""
    t = _normalizar_texto_comando_wa(texto)
    if not _parse_traslado_bodegas(t):
        return False
    frag = _fragmento_insumo_traslado(t)
    if not frag:
        return False
    norm = _normaliza_busqueda_mp(frag)
    if norm in _NOMBRES_MP_GENERICOS:
        return False
    return True


def _es_mensaje_traslado(texto: str) -> bool:
    """Traslado de MP entre bodegas — no confundir con producción de subreceta."""
    t = _normalizar_texto_comando_wa(texto)
    if not t:
        return False
    tl = t.lower()
    # Prefiltro tolerante (typos, unicode raro en móvil)
    if re.search(r"traslad|tralsad|transfer|transfi", tl):
        return True
    if _TRASLADO_VERBS_RE.search(t):
        return True
    if re.search(r"\b(traslado|transferencia)\b", t, re.I):
        return True
    if re.search(r"\bmp\b|materia\s+prima", tl) and _TRASLADO_DE_A_RE.search(t):
        return True
    # «mueve producto», «raslada insumo» sin verbo estándar pero con typo corregido
    if re.search(r"\b(producto|productos|insumo|insumos)\b", tl) and _TRASLADO_VERBS_RE.search(
        t
    ):
        return True
    if _es_traslado_implicito(t):
        return True
    return False


def _parece_intento_traslado(texto: str) -> bool:
    """Incluye typos; usar para bloquear produccion_subreceta en el LLM."""
    return _es_mensaje_traslado(texto)


def _es_contexto_bodegas_no_sub(texto: str) -> bool:
    """Los 0xx en «de 005 a 001» son bodegas, no códigos SUB."""
    if _TRASLADO_DE_A_RE.search(texto or ""):
        return True
    t = (texto or "").lower()
    if re.search(r"\b(?:bod[- ]?)?0\d{2,3}\b", t) and re.search(
        r"\b(?:de|desde|a|hacia)\b", t
    ):
        return True
    return False


def _parse_traslado_bodegas(texto: str) -> dict | None:
    from bodegas_config import nombre_bodega, resolver_cod_bodega

    m = _TRASLADO_DE_A_RE.search(texto or "")
    if not m:
        return None
    orig = resolver_cod_bodega(m.group(1))
    dest = resolver_cod_bodega(m.group(2))
    if not orig or not dest:
        return None
    return {
        "bodega_origen": orig,
        "bodega_destino": dest,
        "nombre_origen": nombre_bodega(orig) or orig,
        "nombre_destino": nombre_bodega(dest) or dest,
    }


def _limpiar_ctx_produccion(wa_id: str) -> None:
    """Evita que un traslado herede pending/last_cods/historial de producción."""
    _pending_prod_sub.pop(wa_id, None)
    _pending_prod_area.pop(wa_id, None)
    _pending_prod_ctx.pop(wa_id, None)
    historiales.pop(wa_id, None)


_NOMBRES_MP_GENERICOS = frozenset({
    "mp",
    "mps",
    "materia",
    "prima",
    "materia prima",
    "producto",
    "productos",
    "insumo",
    "insumos",
    "item",
    "articulo",
})


def _extraer_nombre_mp_traslado(texto: str) -> str:
    """Nombre del insumo en «traslada papa de 005 a 001»; vacío si es genérico."""
    t = (texto or "").strip()
    if not t:
        return ""
    m_bod = _TRASLADO_DE_A_RE.search(t)
    parte = t[: m_bod.start()].strip() if m_bod else t
    m = re.search(
        r"(?:traslad\w*|transfer\w*|transfi\w*|muev\w*|pasar?|pasa)\s+"
        r"(?:(?:una?|un)\s+)?"
        r"(?:(?:mp|materia\s+prima)\s+)?"
        r"(.+)$",
        parte,
        re.I,
    )
    if not m:
        return ""
    cand = m.group(1).strip(" .,;")
    if not cand:
        return ""
    norm = _normaliza_busqueda_mp(cand)
    if norm in _NOMBRES_MP_GENERICOS:
        return ""
    if norm.split()[0] in _NOMBRES_MP_GENERICOS and len(norm.split()) <= 2:
        return ""
    return cand


def _traslado_ctx_get(wa_id: str) -> dict:
    ctx = _pending_traslado.get(wa_id)
    if not ctx:
        return {}
    if time.monotonic() - ctx.get("at", 0) > _TRASLADO_CTX_TTL_SEC:
        _pending_traslado.pop(wa_id, None)
        return {}
    return ctx


def _es_traslado_generico_sin_detalle(texto: str) -> bool:
    """«trasladar producto/mp/materia prima» sin insumo ni bodegas concretas."""
    t = _normalizar_texto_comando_wa(texto).lower().strip(" .,;")
    if not _es_mensaje_traslado(t):
        return False
    if _parse_traslado_bodegas(t):
        return False
    if _resolver_subreceta_para_traslado(t):
        return False
    frag = _texto_item_traslado(t)
    if frag and _normaliza_busqueda_mp(frag) not in _NOMBRES_MP_GENERICOS:
        return False
    return bool(
        re.match(
            r"^(?:traslad\w*|transfer\w*|transfi\w*|muev\w*|pasar?|pasa|pase)\s+"
            r"(?:(?:una?|un)\s+)?"
            r"(?:mp|materia\s+prima|producto?s?|insumo?s?|semi?s?|subrecetas?)?\s*$",
            t,
            re.I,
        )
    )


def _texto_traslado_combinado(wa_id: str, texto: str) -> str:
    """Combina detalle nuevo con traslado genérico pendiente (ej. transferir producto → 5 tortas…)."""
    new = _normalizar_texto_comando_wa(texto)
    ctx = _traslado_ctx_get(wa_id)
    if not ctx:
        return new
    if _es_mensaje_traslado(new) and (_parse_traslado_bodegas(new) or _fragmento_insumo_traslado(new)):
        return new
    prev = (ctx.get("texto") or "").strip()
    if not prev:
        return new
    if _parse_traslado_bodegas(new) or _fragmento_insumo_traslado(new):
        return new
    return new


def _msg_traslado_inicio() -> str:
    return (
        "*Traslado entre bodegas* (sin usar el modelo de IA).\n\n"
        "Dime en un mensaje:\n"
        "1. Qué mueves (MP, subreceta o semi)\n"
        "2. Cantidad (ej. 5 tortas, 1 botella, 750 ml)\n"
        "3. Origen y destino (ej. de cocina a externa, de 005 a 001)\n\n"
        "Ejemplos:\n"
        "• Traslada papa 10 kg de cocina a externa\n"
        "• Traslada 5 tortas de chocolate de cocina a externa\n"
        "• Mueve whisky Buchanan de consignación a barra 750 ml"
    )


def _traslado_ctx_touch(wa_id: str, **updates) -> None:
    ctx = _traslado_ctx_get(wa_id) or {}
    ctx.update(updates)
    ctx["at"] = time.monotonic()
    _pending_traslado[wa_id] = ctx


def _texto_item_traslado(texto: str) -> str:
    """Fragmento con el ítem, sin verbos ni tramo de bodegas."""
    t = (texto or "").strip()
    m = _TRASLADO_DE_A_RE.search(t)
    if m:
        t = t[: m.start()].strip()
    t = _TRASLADO_VERBO_INICIO_RE.sub("", t)
    t = re.sub(r"^(?:(?:una?|un)\s+)?(?:(?:mp|materia\s+prima)\s+)?", "", t, flags=re.I)
    return t.strip(" .,;")


def _es_aclaracion_traslado_sub(texto: str) -> bool:
    t = (texto or "").strip().lower()
    if not t:
        return False
    if t in ("subreceta", "semi", "es sub", "es subreceta", "es una subreceta", "es un semi"):
        return True
    return bool(
        re.search(r"\b(es\s+)?(una?\s+)?(subreceta|sub|semi)\b", t)
        and not re.search(r"\b(producir|preparar)\w*", t)
    )


def _resolver_subreceta_para_traslado(texto: str) -> dict | None:
    """Subreceta semi (SUB-xxx) + cantidad en unidad base para traslado entre bodegas."""
    from descargo_subreceta import pseudo_mp_cod
    from unidades_operativas import resolver_cantidad_produccion_sub

    frag = _texto_item_traslado(texto)
    if not frag or _normaliza_busqueda_mp(frag) in _NOMBRES_MP_GENERICOS:
        frag = _texto_item_traslado(texto) or (texto or "")

    cods = _match_sub_codigos_en_texto(frag) or _match_sub_codigos_en_texto(texto)
    if not cods:
        return None

    cod = cods[0]
    conv = resolver_cantidad_produccion_sub(cod, None, texto=texto)
    cant = conv.get("cantidad_base")
    if cant is None:
        rend = float(conv.get("rendimiento_estandar") or 0)
        cant = rend if rend > 0 else None

    nombre = cod
    unidad = "gr"
    try:
        from codigos_subreceta import cod_sub_canonico
        from subrecetas_detalle import cargar_bd_subrecetas

        info = cargar_bd_subrecetas(conectar_sheets()).get(cod_sub_canonico(cod), {})
        nombre = (info.get("nombre_subreceta") or cod).strip()
        unidad = (info.get("unidad") or info.get("unidad_base") or "gr").strip()
    except Exception:
        pass

    return {
        "cod_sub": cod,
        "cod_mp": pseudo_mp_cod(cod),
        "nombre": nombre,
        "cantidad": cant,
        "unidad": unidad,
        "interpretacion": conv.get("interpretacion") or "",
    }


async def _manejar_aclaracion_traslado(wa_id: str, texto: str, msg: dict | None) -> bool:
    if not _es_aclaracion_traslado_sub(texto):
        return False
    ctx = _traslado_ctx_get(wa_id)
    if not ctx:
        return False
    texto_orig = (ctx.get("texto") or ctx.get("nombre") or "").strip()
    if not texto_orig:
        return False
    await _manejar_traslado_mp_wa(wa_id, texto_orig, msg, forzar_sub=True)
    return True


async def _manejar_consulta_ventas_wa(
    wa_id: str,
    msg: dict | None,
    texto: str = "",
    *,
    incluir_productos: bool = True,
) -> None:
    """Ventas del día — directo desde Supabase/Smart Menu, sin LLM."""
    try:
        fecha = _fecha_consulta_ventas_simple(texto)
        await _feedback_procesando(wa_id, msg)
        r = await asyncio.to_thread(
            tool_ventas_dia,
            {
                "fecha": fecha,
                "incluir_productos": incluir_productos,
                "limite": 5,
            },
        )
        out = (r.get("texto_whatsapp") or "").strip() if isinstance(r, dict) else ""
        if not out:
            await enviar_mensaje_meta(wa_id, "No hay datos de ventas para hoy.")
            return
        await _enviar_texto_largo_wa(wa_id, out)
    except Exception as e:
        print(f"[Meta] consulta ventas wa_id={wa_id!r} texto={texto!r}: {e}")
        await enviar_mensaje_meta(
            wa_id,
            "No pude consultar ventas. Intenta de nuevo en un momento.",
        )


async def _manejar_consulta_receta_plato_wa(
    wa_id: str, texto: str, msg: dict | None
) -> None:
    """Receta / ingredientes de plato fuerte — directo desde Sheets, sin LLM."""
    nombre = _extraer_nombre_plato_receta(texto)
    if not nombre:
        await enviar_mensaje_meta(
            wa_id,
            "¿De qué plato quieres la receta o los ingredientes?\n"
            "Ej: bibimbap, bao de langosta, tarta vasca, negroni",
        )
        return

    await _feedback_procesando(wa_id, msg)
    ocultar = not puede_ver_costos(wa_id)
    try:
        r = await asyncio.to_thread(
            tool_receta_ingredientes,
            {
                "nombre_plato": nombre,
                "incluir_ingredientes": True,
                "_ocultar_costos": ocultar,
            },
        )
    except Exception as e:
        print(f"[Meta] consulta receta plato wa_id={wa_id!r}: {e}")
        await enviar_mensaje_meta(
            wa_id,
            f"No pude consultar la receta de «{nombre}». Intenta con el nombre exacto del menú.",
        )
        return

    if not isinstance(r, dict):
        await enviar_mensaje_meta(wa_id, "No pude interpretar la consulta de receta.")
        return

    texto_out = (r.get("texto_whatsapp") or "").strip()
    if texto_out:
        await _enviar_texto_largo_wa(wa_id, texto_out)
        return

    if r.get("ambiguo") and r.get("opciones"):
        lines = [f"Varias coincidencias para «{nombre}»:", ""]
        for op in r["opciones"][:12]:
            nom = (op.get("nombre_receta") or "").strip()
            var = (op.get("variedad_smart_menu") or "").strip()
            cod = (op.get("cod_receta") or "").strip()
            line = f"• {nom}" + (f" ({var})" if var else "")
            if cod:
                line += f" [{cod}]"
            lines.append(line)
        lines.append("\nRepite con el nombre exacto o la variedad.")
        await enviar_mensaje_meta(wa_id, "\n".join(lines))
        return

    await enviar_mensaje_meta(
        wa_id,
        (r.get("mensaje") or r.get("error") or f"No encontré «{nombre}» en BD_RECETAS_DETALLE.")
        + "\n\nPrueba otro nombre del menú (ej. BIBIMBAP COREANO).",
    )


async def _manejar_traslado_mp_wa(
    wa_id: str,
    texto: str,
    msg: dict | None,
    *,
    forzar_sub: bool = False,
) -> None:
    """Traslados MP o subreceta semi (SUB-xxx) — sin LLM."""
    from unidades_operativas import parse_cantidad_explicita_base, parse_cantidad_presentacion

    _limpiar_ctx_produccion(wa_id)
    texto = _normalizar_texto_comando_wa(texto)
    bod = _parse_traslado_bodegas(texto)
    sub = _resolver_subreceta_para_traslado(texto)
    if not sub and forzar_sub:
        sub = _resolver_subreceta_para_traslado(_texto_item_traslado(texto))
    nombre = _extraer_nombre_mp_traslado(texto)
    if sub:
        nombre = ""

    _traslado_ctx_touch(wa_id, texto=texto, bod=bod, nombre=nombre, sub=sub)

    generico = not bod and not nombre and not sub
    if not generico:
        await _feedback_procesando(wa_id, msg)

    if generico:
        out = (
            _msg_traslado_inicio()
            if _es_traslado_generico_sin_detalle(texto)
            else (
                "Para trasladar dime:\n"
                "1. Qué insumo o subreceta (ej. papa, torta de chocolate, SUB-010)\n"
                "2. De qué bodega a cuál (ej. de cocina a externa, o de 005 a 001)\n"
                "3. Cantidad (ej. 5 tortas, 1 botella, 750 ml)\n\n"
                "Ejemplo: Traslada 5 tortas de chocolate de cocina a externa"
            )
        )
        await enviar_mensaje_meta(wa_id, out)
        return

    if not bod:
        etiqueta = (sub or {}).get("nombre") or nombre or "el producto"
        out = (
            f"Insumo/subreceta: {etiqueta}.\n"
            "¿De qué bodega a cuál? Ej: de cocina a externa "
            "o de consignación a barra."
        )
        await enviar_mensaje_meta(wa_id, out)
        return

    if not nombre and not sub:
        out = (
            f"Traslado {bod['nombre_origen']} → {bod['nombre_destino']}.\n"
            "¿Qué mueves? Nombre de MP o subreceta (ej. papa, torta de chocolate)."
        )
        await enviar_mensaje_meta(wa_id, out)
        return

    cod_mp = None
    nombre_mp = nombre
    interpretacion = ""
    cantidad = 1.0

    if sub:
        cod_mp = sub["cod_mp"]
        nombre_mp = sub["nombre"]
        if sub.get("cantidad"):
            cantidad = float(sub["cantidad"])
        interpretacion = sub.get("interpretacion") or ""
    else:
        expl = parse_cantidad_explicita_base(texto)
        pres = parse_cantidad_presentacion(texto)
        if expl is not None:
            cantidad = expl
        elif pres:
            cantidad = pres[0]

    args: dict = {
        "bodega_origen": bod["bodega_origen"],
        "bodega_destino": bod["bodega_destino"],
        "cantidad": cantidad,
        "confirmado": False,
        "texto_original": texto,
    }
    if cod_mp:
        args["cod_mp_sistema"] = cod_mp
        args["nombre_mp"] = nombre_mp
    else:
        args["nombre_mp"] = nombre_mp

    try:
        r = await asyncio.to_thread(tool_trasladar_mp, args)
    except Exception as e:
        await enviar_mensaje_meta(wa_id, f"Error al planificar traslado: {e}")
        return

    if r.get("error") and not sub:
        sub2 = _resolver_subreceta_para_traslado(texto) or _resolver_subreceta_para_traslado(
            nombre or ""
        )
        if sub2:
            await _manejar_traslado_mp_wa(wa_id, texto, msg, forzar_sub=True)
            return
        out = (
            f"No encontré «{nombre_mp}» como materia prima.\n"
            "Si es una *subreceta* (semi en inventario), responde: es subreceta\n"
            "o repite con el nombre exacto de BD_SUBRECETAS (ej. torta de chocolate)."
        )
        await enviar_mensaje_meta(wa_id, out)
        return

    if r.get("requiere_eleccion"):
        out = r.get("mensaje") or "Varios productos coinciden; indica cuál por nombre."
    elif r.get("requiere_confirmacion"):
        out = r.get("mensaje") or "Confirma el traslado."
        if interpretacion and interpretacion not in out:
            out = f"{interpretacion}\n\n{out}"
    elif r.get("error"):
        out = str(r.get("error"))
    elif r.get("mensaje"):
        out = r["mensaje"]
    else:
        out = str(r)
    _pending_traslado.pop(wa_id, None)
    await enviar_mensaje_meta(wa_id, out)


_BATCH_KEYWORDS = (
    "batch",
    "subreceta",
    "sub receta",
    "sub-0",
    "semi",
    "salsa",
    "pan bao",
    "bao",
    "masa",
    "prepar",
    "produc",
    "registr",
    "hacer",
)


def _parse_batch_lenguaje_natural(texto: str, wa_id: str | None = None) -> dict | None:
    """Detecta pedidos de producción en lenguaje natural (barra o cocina)."""
    from estrategia_config import bodega_default_produccion_sub

    raw = (texto or "").strip()
    if not raw:
        return None
    if _es_mensaje_conteo(raw, wa_id):
        return None
    if _es_consulta_lista_subrecetas(raw):
        return None
    if _es_consulta_receta_plato(raw):
        return None
    if _es_mensaje_traslado(raw):
        return None
    t = raw.lower()
    cods = _match_sub_codigos_en_texto(raw)
    parece_batch = (
        any(k in t for k in _BATCH_KEYWORDS)
        or bool(cods)
        or (
            _es_intento_produccion(raw, wa_id)
            and bool(_prod_ctx_get(wa_id or "").get("area"))
        )
    )
    if not parece_batch:
        return None
    area = _resolver_area_produccion(wa_id, raw, cods=cods)
    bod = _bodega_por_area(area) or (
        bodega_default_produccion_sub(wa_id) if wa_id else "BOD-002"
    )
    if not cods:
        return {
            "cods": [],
            "bodega": bod,
            "confirmar": False,
            "ambiguo": True,
            "area": area,
        }
    cantidad = _extraer_cantidad_sub(raw, cod_sub=cods[0] if cods else None)
    confirmar = any(
        w in t
        for w in ("confirmar", "confirmo", "aplicar", "de verdad", "en serio")
    )
    m = re.search(r"\b(bod-\d{3})\b", raw, re.I)
    if m:
        bod = m.group(1).upper()
    area = _resolver_area_produccion(wa_id, raw, cods=cods, area_hint=area)
    if area:
        bod = _bodega_por_area(area) or bod
    return {
        "cods": cods,
        "bodega": bod,
        "confirmar": confirmar,
        "cantidad": cantidad,
        "area": area,
    }


def _prod_ctx_update_from_parse(wa_id: str, texto: str, prod_sub: dict) -> None:
    cods = prod_sub.get("cods") or []
    area = _resolver_area_produccion(wa_id, texto, cods=cods, area_hint=prod_sub.get("area"))
    upd: dict = {}
    if area:
        upd["area"] = area
    if cods:
        upd["last_cods"] = cods
    if upd:
        _prod_ctx_touch(wa_id, **upd)


def _resolver_prod_sub(texto: str, wa_id: str) -> dict | None:
    if _es_mensaje_traslado(texto):
        return None
    ctx = _prod_ctx_get(wa_id)

    if _es_confirmacion_produccion(texto) and wa_id in _pending_prod_sub:
        return {**_pending_prod_sub[wa_id], "confirmar": True}

    prod_sub = _parse_producir_sub_comando(texto, wa_id)
    if prod_sub is None:
        prod_sub = _parse_batch_lenguaje_natural(texto, wa_id)

    if (prod_sub is None or not prod_sub.get("cods")) and _es_orden_produccion_afirmativa(texto):
        from estrategia_config import bodega_default_produccion_sub

        cods = _match_sub_codigos_en_texto(texto) or list(ctx.get("last_cods") or [])
        if cods:
            area = _resolver_area_produccion(wa_id, texto, cods=cods)
            cantidad = _extraer_cantidad_sub(texto, cod_sub=cods[0])
            prod_sub = {
                "cods": cods,
                "bodega": _bodega_por_area(area)
                or (bodega_default_produccion_sub(wa_id) if wa_id else "BOD-002"),
                "cantidad": cantidad,
                "confirmar": False,
                "area": area,
            }

    if prod_sub is None and ctx.get("area"):
        from estrategia_config import bodega_default_produccion_sub

        cods = _match_sub_codigos_en_texto(texto)
        cantidad = _extraer_cantidad_sub(texto, cod_sub=cods[0])
        if cods and (cantidad is not None or _es_intento_produccion(texto)):
            area = _resolver_area_produccion(wa_id, texto, cods=cods)
            prod_sub = {
                "cods": cods,
                "bodega": _bodega_por_area(area) or _bodega_por_area(ctx["area"]),
                "cantidad": cantidad,
                "confirmar": False,
                "area": area or ctx.get("area"),
            }

    if prod_sub is not None:
        _prod_ctx_update_from_parse(wa_id, texto, prod_sub)
    return prod_sub


def _es_comando_conteo(texto_upper: str) -> bool:
    return (
        texto_upper == "APROBAR TODO"
        or texto_upper.startswith("APROBAR ")
        or texto_upper.startswith("RECHAZAR ")
        or texto_upper.startswith("KARDEX ")
        or texto_upper.startswith("CSV ")
    )


def _es_confirmacion_produccion(texto: str) -> bool:
    t = (texto or "").strip().lower().replace("í", "i")
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
    ):
        return True
    return t.startswith("si ") and "confirm" in t


def _parse_iniciar_conteo_comando(texto: str) -> str | None:
    """INICIAR CONTEO BOD-001 → cod_bodega o None si no aplica."""
    t = (texto or "").strip().upper()
    if not t.startswith("INICIAR CONTEO"):
        return None
    resto = texto.strip()[len("INICIAR CONTEO") :].strip()
    if not resto:
        return ""
    return resto.split()[0].strip()


# ── Definición tools Claude API ──────────────────────────────
TOOLS = [
    {"name": "ventas_hoy", "description": "Ventas del dia actual: total oficial Smart Menu, tickets, top 5 platos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "ventas_semana", "description": "Ventas de la semana actual lunes a hoy: total oficial, promedio diario, top 5 platos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "stock_critico", "description": "Listado de insumos bajo par level (par_level>0 y stock_actual<par_level) ordenado por deficit_pct. Por defecto devuelve TODO; puedes pasar top para truncar.", "input_schema": {"type": "object", "properties": {"top": {"type": "integer"}}, "required": []}},
    {"name": "stocks_negativos", "description": "Listado de materias primas con stock_actual negativo en BD_MP_SISTEMA. No inventa datos.", "input_schema": {"type": "object", "properties": {"top": {"type": "integer"}}, "required": []}},
    {"name": "inventario_valorizado", "description": "Valorización de inventario (stock_actual*costo_unitario_ref) desde BD_MP_SISTEMA. Si el usuario pide el valorizado de un insumo concreto (ej. camarones), pasa nombre_mp o buscar con esa palabra: busca por substring en nombre_mp sin importar tildes y singular/plural simple. Con nombre_mp se listan también MPs sin costo de referencia o con stock 0 para explicar por qué no hay valor.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}, "buscar": {"type": "string"}, "cod_bodega": {"type": "string"}, "top": {"type": "integer"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}, "incluir_cero": {"type": "boolean"}}, "required": []}},
    {"name": "inventario_por_bodega", "description": "Resumen de valor (USD) y stock total por bodega desde BD_MP_SISTEMA.", "input_schema": {"type": "object", "properties": {"incluir_sin_costo": {"type": "boolean"}}, "required": []}},
    {"name": "facturas_parciales", "description": "Facturas con estado PARCIAL en Supabase (facturas_procesadas).", "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": []}},
    {"name": "items_pendientes_factura", "description": "Ítems pendientes (BD_ITEMS_PENDIENTES) filtrando por num_factura o ruc_proveedor.", "input_schema": {"type": "object", "properties": {"num_factura": {"type": "string"}, "ruc_proveedor": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": []}},
    {"name": "compras_facturas_rango", "description": "Compras/gasto por proveedor: SOLO lineas ya ingresadas al inventario (mov_inventario ENTRADA), no el total XML de la factura si hubo lineas sin match. Devuelve texto_whatsapp: copialo tal cual. Fechas YYYY-MM-DD o mes_nombre+anio (ej. mayo 2026). razon_social desde BD_PROV; si falta nombre indica facturas_ejemplo.", "input_schema": {"type": "object", "properties": {"fecha_desde": {"type": "string"}, "fecha_hasta": {"type": "string"}, "mes_nombre": {"type": "string"}, "anio": {"type": "integer"}, "mes": {"type": "integer"}, "nombre_proveedor": {"type": "string"}, "ruc_proveedor": {"type": "string"}, "top_facturas": {"type": "integer"}, "top_productos": {"type": "integer"}}, "required": []}},
    {"name": "compras_factura_detalle", "description": "Lineas EXACTAS ingresadas al inventario de UNA factura de compra (mov_inventario). Devuelve texto_whatsapp: copialo tal cual. Para ultima factura de un proveedor: ultima=true con nombre_proveedor (ej. Maramar) o ruc_proveedor. Para una factura concreta: num_factura. NUNCA inventes productos ni cantidades.", "input_schema": {"type": "object", "properties": {"num_factura": {"type": "string"}, "nombre_proveedor": {"type": "string"}, "ruc_proveedor": {"type": "string"}, "ultima": {"type": "boolean"}}, "required": []}},
    {"name": "mp_incompletas", "description": "MPs con datos incompletos en BD_MP_SISTEMA (sin_costo, sin_par, sin_bodega).", "input_schema": {"type": "object", "properties": {"tipo": {"type": "string", "enum": ["sin_costo","sin_par","sin_bodega"]}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": []}},
    {"name": "resumen_operativo_hoy", "description": "Resumen compacto: ventas hoy + bajo par + negativos + facturas parciales.", "input_schema": {"type": "object", "properties": {"top": {"type": "integer"}}, "required": []}},
    {"name": "pedidos_hoy", "description": "Pedidos que corresponde hacer hoy segun ventana de cada proveedor.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "plato_top_semana", "description": "Top 10 platos mas vendidos esta semana por cantidad.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "buscar_bodega", "description": "En que bodega esta un insumo. Busca por nombre_mp (como lo dice el usuario). Si hay varios parecidos devuelve requiere_eleccion: pregunta al usuario por NOMBRE, sin pedir codigos.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "trasladar_mp", "description": "Trasladar insumo entre bodegas. Usa nombre_mp (obligatorio salvo que ya resolviste el producto en el turno anterior). NUNCA inventes cod_mp_sistema. Si requiere_eleccion, pregunta al usuario cual producto por nombre. confirmado=false primero, luego true tras confirmacion. CANTIDADES: el inventario está en unidad_base (gr/ml/uni). Si el usuario dice botella/caja/pack/lata o un número pequeño de unidades de compra (ej. «una botella de Buchanan's Master»), pasa cantidad=1 (o N) y unidad_presentacion=botella; el sistema convierte con factor_conversion del catálogo (ej. 750 ml). Si dice ml/gr explícitos, pasa esa cantidad en unidad base.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string", "description": "Nombre del producto como lo dice el usuario (ej. Buchanan 18, papa)"}, "cod_mp_sistema": {"type": "string", "description": "Solo si la tool anterior devolvio opciones y ya eligio el usuario; no inventar"}, "bodega_origen": {"type": "string"}, "bodega_destino": {"type": "string"}, "cantidad": {"type": "number", "description": "Cantidad pedida (unidad base o unidades de compra si va con unidad_presentacion)"}, "cantidad_presentacion": {"type": "number", "description": "Ej. 1 para «una botella», 6 para «6 cajas»"}, "unidad_presentacion": {"type": "string", "description": "botella, caja, pack, lata, etc."}, "unidad_pedida": {"type": "string", "description": "Alias de unidad_presentacion"}, "texto_original": {"type": "string", "description": "Frase del usuario para interpretar cantidades naturales"}, "confirmado": {"type": "boolean"}}, "required": ["nombre_mp","bodega_origen","bodega_destino","cantidad","confirmado"]}},
    {"name": "ventas_por_plato", "description": "Ventas al cliente (hist_ventas): total del periodo + ranking SOLO productos en BD_PRODUCTOS (carta) si incluir_productos=true. Para un mes pasado usa mes_nombre (ej. mayo) + anio, o fecha_ini/fecha_fin ISO — NO uses periodo=mes (eso es solo el mes calendario en curso). periodo hoy/semana/mes actual. Devuelve texto_whatsapp: copialo tal cual.", "input_schema": {"type": "object", "properties": {"periodo": {"type": "string", "enum": ["hoy","semana","mes"]}, "fecha_ini": {"type": "string"}, "fecha_fin": {"type": "string"}, "anio": {"type": "integer"}, "mes": {"type": "integer", "description": "1-12"}, "mes_nombre": {"type": "string", "description": "ej. mayo, junio"}, "orden": {"type": "string", "enum": ["usd", "cantidad"]}, "limite": {"type": "integer"}, "incluir_productos": {"type": "boolean", "description": "Si false: responde solo total del periodo y pregunta si quiere detalle de productos."}, "sin_truncar": {"type": "boolean", "description": "Si true: incluye todos los productos (sin '... y N más')."}}, "required": []}},
    {"name": "rotacion_baja", "description": "Productos con nula o baja rotacion en los ultimos N dias.", "input_schema": {"type": "object", "properties": {"dias": {"type": "integer"}, "umbral_unidades": {"type": "number"}}, "required": []}},
    {"name": "stock_ingrediente", "description": "Stock de un insumo por nombre_mp (como lo dice el usuario). Si varios parecidos: requiere_eleccion y pregunta por nombre, sin codigos MP.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "consumo_ingrediente_recetas", "description": "Consumo teorico de una materia prima segun ventas (hist_ventas estado_match PROCESADO) y gramajes en BD_RECETAS_DETALLE; misma logica que el descargo de inventario; NO es stock en bodega. Devuelve total_consumo_teorico y por_plato (lista completa por nombre_producto de venta). No inventes filas ni subtotales: la suma de consumo_mp en por_plato debe coincidir con total_consumo_teorico. nombre_mp obligatorio. Periodo: semana (default lunes a hoy), mes, hoy; o fecha_ini y fecha_fin ISO.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}, "periodo": {"type": "string", "enum": ["semana", "mes", "hoy"]}, "fecha_ini": {"type": "string"}, "fecha_fin": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "costo_plato", "description": "Costo teorico en USD de preparar 1 plato vendido (food cost estandar): suma MPs y subrecetas en BD_RECETAS_DETALLE. Por defecto devuelve texto_whatsapp con desglose; si incluir_ingredientes=false responde solo el total y pregunta si quieres ingredientes.", "input_schema": {"type": "object", "properties": {"cod_receta": {"type": "string"}, "nombre_plato": {"type": "string"}, "nombre_receta": {"type": "string"}, "variedad_smart_menu": {"type": "string"}, "variedad": {"type": "string"}, "incluir_ingredientes": {"type": "boolean"}, "top": {"type": "integer", "description": "Cuantos ingredientes principales listar cuando incluir_ingredientes=true (default 25)."}}, "required": []}},
    {"name": "receta_ingredientes", "description": "Ingredientes y costos de un plato vendido (cantidades por 1 unidad + USD por linea y total). Misma logica que costo_plato; usar cuando pidan receta, ingredientes, gramajes o desglose de un plato (ej. TARTA VASCA, BAO). cod_receta o nombre_plato; opcional variedad.", "input_schema": {"type": "object", "properties": {"cod_receta": {"type": "string"}, "nombre_plato": {"type": "string"}, "nombre_receta": {"type": "string"}, "variedad_smart_menu": {"type": "string"}, "variedad": {"type": "string"}}, "required": []}},
    {"name": "costo_subreceta", "description": "Costo teorico del lote estandar de una subreceta (BD_SUBRECETAS_DETALLE): MPs y subrecetas hijas con cantidades, unidad_base, costo_unitario y costo_linea; total lote y costo por unidad de rendimiento. Usar para salsas, masas, rellenos, etc. Pasa cod_subreceta (ej. 010) o nombre_subreceta (substring).", "input_schema": {"type": "object", "properties": {"cod_subreceta": {"type": "string"}, "nombre_subreceta": {"type": "string"}}, "required": []}},
    {"name": "listar_subrecetas", "description": "Lista COMPLETA de subrecetas activas en BD_SUBRECETAS (barra batches 051-054 en BOD-002 y cocina 002-050/055-059 en BOD-001). Usar cuando pregunten que subrecetas hay, catalogo, lista completa, todas las subs. Devuelve texto_whatsapp: copialo tal cual sin omitir filas. area opcional: barra o cocina.", "input_schema": {"type": "object", "properties": {"area": {"type": "string", "enum": ["barra", "cocina"]}, "texto": {"type": "string", "description": "Mensaje original del usuario para inferir area"}}, "required": []}},
    {"name": "auditar_costos_recetas", "description": "Auditoria de costos de platos inflados y lineas MP sospechosas en recetas (precio/kg mal como USD/gr, garnish caro en bebidas, sin costo). Usar cuando pidan revisar costos de carta, platos raros caros, o validar recetas vs costos. Devuelve top platos_inflados y lineas_mp_sospechosas con flags.", "input_schema": {"type": "object", "properties": {"umbral_plato": {"type": "number"}, "umbral_linea": {"type": "number"}, "top_platos": {"type": "integer"}, "top_lineas": {"type": "integer"}}, "required": []}},
    {"name": "ventas_dia", "description": "Ventas de un dia (fecha YYYY-MM-DD; default hoy): total oficial, tickets, dia_semana y etiqueta_fecha calculados. Devuelve texto_whatsapp: copialo tal cual. Si incluir_productos=false (default): solo total + pregunta si quiere detalle de platos. Si incluir_productos=true: incluye ranking en platos (orden total_usd desc; limite si piden top N).", "input_schema": {"type": "object", "properties": {"fecha": {"type": "string"}, "limite": {"type": "integer"}, "incluir_productos": {"type": "boolean", "description": "Si false: solo total del dia y pregunta si quiere detalle de productos/platos."}}, "required": []}},
    {"name": "ventas_por_dia", "description": "Desglose de ventas POR CADA DIA en un mes o rango: total diario y tickets. Usar cuando pidan valor por dia, desglose diario, ventas dia a dia de un mes (ej. mayo). Pasa mes_nombre=mayo + anio, o fecha_ini/fecha_fin (2026-05-01 a 2026-05-31). Devuelve texto_whatsapp: copialo tal cual. NO usar ventas_por_plato para esto.", "input_schema": {"type": "object", "properties": {"fecha_ini": {"type": "string"}, "fecha_fin": {"type": "string"}, "anio": {"type": "integer"}, "mes": {"type": "integer"}, "mes_nombre": {"type": "string"}, "periodo": {"type": "string", "enum": ["hoy","semana","mes"], "description": "Solo mes/semana/hoy actual si no pasas mes_nombre ni fechas."}, "incluir_dias_cero": {"type": "boolean", "description": "Si true, lista dias sin ventas con 0 USD."}}, "required": []}},
    {"name": "conteo_iniciar", "description": "Inicia inventario físico cíclico: crea conteo_ciclo en Supabase, carga snapshot de MPs de la bodega y genera pestaña CONTEO/CONTEO_BARRA en el maestro Sheets. Usar cuando pidan empezar conteo, toma de inventario, inventario físico de cocina o barra. cod_bodega obligatorio (BOD-001 cocina, BOD-002 barra). semana_iso/anio opcionales (default semana ISO actual). Devuelve ciclo_id, URL de la hoja e instrucciones.", "input_schema": {"type": "object", "properties": {"cod_bodega": {"type": "string"}, "anio": {"type": "integer"}, "semana_iso": {"type": "integer"}, "sheet_name": {"type": "string"}, "reemplazar_snapshot": {"type": "boolean"}, "sobreescribir_hoja": {"type": "boolean"}, "responsable_nombre": {"type": "string"}, "notas": {"type": "string"}}, "required": ["cod_bodega"]}},
    {"name": "conteo_listar_ciclos", "description": "Lista ciclos de inventario físico en Supabase (conteo_ciclo). Filtros opcionales estado y cod_bodega.", "input_schema": {"type": "object", "properties": {"estado": {"type": "string"}, "cod_bodega": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}},
    {"name": "conteo_ciclos_abiertos", "description": "Resumen de ciclos de conteo que NO están CONTABILIZADO ni ANULADO (borradores activos).", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "produccion_subreceta", "description": "Registra preparación/producción de subreceta(s) en barra o cocina: baja MPs del detalle y entra stock del semi (SUB-xxx) en mov_inventario. Por defecto SIMULA (no escribe); usar simular=false o comando con CONFIRMAR para aplicar. Eduardo barra: bodega BOD-002, subs 051-054. cod_subreceta puede ser lista o string. CANTIDADES: inventario del semi en gr/ml según rendimiento_estandar del lote. Si el usuario dice «una torta», «6 tortas de chocolate» o «un lote», pasa cantidad_lotes o cantidad=N y el sistema multiplica por rendimiento_estandar (ej. torta chocolate 1054 gr). Si dice gr/ml explícitos, pasa cantidad en unidad base. Sin cantidad → un lote estándar. Devuelve texto_whatsapp.", "input_schema": {"type": "object", "properties": {"cod_subreceta": {"type": "array", "items": {"type": "string"}, "description": "Códigos 051 052 etc."}, "codigos": {"type": "array", "items": {"type": "string"}}, "bodega": {"type": "string", "description": "BOD-002 barra (default) o BOD-001 cocina"}, "cantidad": {"type": "number", "description": "ml/gr/unidades producidas; o número de lotes si es entero pequeño"}, "cantidad_lotes": {"type": "number", "description": "Ej. 6 para «6 tortas»"}, "texto_original": {"type": "string"}, "simular": {"type": "boolean", "description": "true=solo muestra plan (default true para evaluar)"}, "forzar": {"type": "boolean", "description": "true=registrar aunque falte stock MP"}, "confirmar": {"type": "boolean"}, "registrado_por": {"type": "string"}, "recalcular": {"type": "boolean"}}, "required": ["cod_subreceta"]}},
]

TOOL_FNS = {
    "ventas_hoy":       lambda a: tool_ventas_hoy(),
    "ventas_semana":    lambda a: tool_ventas_semana(),
    "stock_critico":    tool_stock_critico,
    "stocks_negativos": tool_stocks_negativos,
    "inventario_valorizado": tool_inventario_valorizado,
    "inventario_por_bodega": tool_inventario_por_bodega,
    "facturas_parciales": tool_facturas_parciales,
    "items_pendientes_factura": tool_items_pendientes_factura,
    "compras_facturas_rango": tool_compras_facturas_rango,
    "compras_factura_detalle": tool_compras_factura_detalle,
    "mp_incompletas": tool_mp_incompletas,
    "resumen_operativo_hoy": tool_resumen_operativo_hoy,
    "pedidos_hoy":      lambda a: tool_pedidos_hoy(),
    "plato_top_semana": lambda a: tool_plato_top_semana(),
    "buscar_bodega":    tool_buscar_bodega,
    "trasladar_mp":     tool_trasladar_mp,
    "ventas_por_plato": tool_ventas_por_plato,
    "rotacion_baja":    tool_rotacion_baja,
    "stock_ingrediente": tool_stock_ingrediente,
    "consumo_ingrediente_recetas": tool_consumo_ingrediente_recetas,
    "costo_plato": tool_costo_plato,
    "receta_ingredientes": tool_receta_ingredientes,
    "costo_subreceta": tool_costo_subreceta,
    "listar_subrecetas": tool_listar_subrecetas,
    "auditar_costos_recetas": lambda a: tool_auditar_costos_recetas(a),
    "ventas_dia":        tool_ventas_dia,
    "ventas_por_dia":    tool_ventas_por_dia,
    "conteo_iniciar": tool_conteo_iniciar,
    "conteo_listar_ciclos": tool_conteo_listar_ciclos,
    "conteo_ciclos_abiertos": lambda a: tool_conteo_ciclos_abiertos(),
    "produccion_subreceta": tool_produccion_subreceta,
}

SYSTEM = """Eres el agente de gestion de Tatami Bao Bar, gastrobar asiatico en Cuenca, Ecuador.
Respondes preguntas sobre ventas, inventario, bodegas y pedidos con datos reales del sistema.
Responde siempre en espanol, de forma clara y directa, como si hablaras con el socio del restaurante.
Usa los datos exactos de las tools. Si no hay datos dilo claramente.
Regla estricta VENTAS vs COMPRAS: si preguntan cuanto se vendio, ventas del mes/semana, productos mas vendidos al cliente, usa las tools de ventas. NO uses compras_facturas_rango salvo que pregunten explicitamente compras a proveedores o facturas de compra.
Mes pasado o con nombre (ej. mayo, abril): usa mes_nombre + anio (ej. mayo 2026 → mes_nombre=mayo, anio=2026) o fecha_ini/fecha_fin ISO. periodo=mes en ventas_por_plato/ventas_por_dia significa SOLO el mes calendario en curso (1 de este mes a hoy), nunca un mes pasado.
Si piden ventas POR DIA o desglose diario de un mes/rango (ej. "valor por dia en mayo", "cuanto se vendio cada dia"): usa ventas_por_dia con mes_nombre/anio o fecha_ini/fecha_fin y copia texto_whatsapp. NO uses ventas_por_plato para desglose diario.
Si el usuario pide SOLO la cifra de ventas (total del periodo, sin desglose diario):
- Un dia concreto (hoy, ayer, fecha): `ventas_dia` con incluir_productos=false y copia literalmente `texto_whatsapp` (usa dia_semana y etiqueta_fecha del JSON; no calcules el dia de la semana).
- Periodo hoy/semana/mes: `ventas_por_plato` con incluir_productos=false y copia `texto_whatsapp`.
Si pide ranking/detalle/productos mas vendidos de un dia: `ventas_dia` con incluir_productos=true (limite si piden top N) y copia `texto_whatsapp`.
Si pide ranking de un periodo (semana/mes): `ventas_por_plato` con incluir_productos=true y copia `texto_whatsapp`.
NUNCA inventes productos (ej. TATAMI WINGS, EDAMAME) que no esten en `ranking` o `platos` de la tool.
Si la lista es larga y no hay texto_whatsapp, continua en mensajes siguientes sin inventar filas.
Si te piden listados de stock negativo, usa la tool stocks_negativos (no adivines nombres ni cantidades).
Si te piden productos bajo par level, usa la tool stock_critico y devuelve el listado completo salvo que el usuario pida \"top N\".
Si te piden valorizacion de inventario, usa inventario_valorizado (y si preguntan por bodegas usa inventario_por_bodega).
Si piden el valorizado de un producto o materia prima por nombre (ej. camarones, aceite), llama inventario_valorizado con nombre_mp o buscar igual al texto que dio el usuario; no listes solo el top global sin filtrar por nombre.
Si te piden facturas pendientes/parciales, usa facturas_parciales e items_pendientes_factura.
Si preguntan compras a proveedores, gasto por proveedor o productos comprados en un periodo, usa compras_facturas_rango (mes_nombre+anio o fecha_desde/fecha_hasta) y copia texto_whatsapp. Esos montos son SOLO lo ingresado al inventario (mov_inventario), no el total de la factura XML si hubo lineas sin match; dilo asi si preguntan que incluye. Si nombran el proveedor (ej. Maramar), pasa nombre_proveedor. Proveedor solo con RUC: usa facturas_ejemplo del JSON para orientar; no inventes nombres.
Si piden detalle de UNA factura, lineas ingresadas, cantidades o valores de la ultima factura de un proveedor, usa compras_factura_detalle (ultima=true + nombre_proveedor, o num_factura) y responde copiando literalmente texto_whatsapp.
La ultima factura de un proveedor es la de mayor fecha_factura en facturas_procesadas, no la de mayor fecha_proceso.
Si el listado es largo y el usuario pidio TODO el detalle (ej. todos los platos vendidos con cantidades y montos), enumera el listado COMPLETO que devuelve la tool sin acortar a top 10. Si no cabe en un mensaje, continua en mensajes siguientes numerados.
Para resumenes cortos puede bastar un parrafo; para pedidos explicitos de detalle completo, no resumas.
Si preguntan cuanto se consumio de un ingrediente o materia prima en un periodo segun las recetas de los platos vendidos (no el stock en bodega), usa la tool consumo_ingrediente_recetas. No digas que el sistema no puede cruzar ventas con recetas: esa tool existe.
Con consumo_ingrediente_recetas: enumera TODAS las filas de por_plato que devuelve la tool (nombres vienen de hist_ventas). El total de consumo en gramos debe ser exactamente total_consumo_teorico; no sumes de cabeza cifras inventadas ni mezcles con otros periodos. Si un nombre de plato no corresponde al menu real, dilo: los datos vienen de ventas y recetas enlazadas; puede haber producto mal nombrado, receta incorrecta o matching viejo.
Si preguntan cuanto cuesta hacer/preparar un plato (food cost, costo de receta, margen teorico del plato):
- Si piden SOLO el costo ("costo de lomo kuro", "cuanto cuesta hacer X"): llama costo_plato con incluir_ingredientes=false y responde con el texto_whatsapp (solo total + pregunta si quiere ingredientes).
- Si piden ingredientes/detalle/desglose: llama costo_plato con incluir_ingredientes=true (opcional top) y responde con el texto_whatsapp.
Si piden ingredientes, gramajes, cantidades o desglose con costos de un plato (receta de venta), usa receta_ingredientes (o costo_plato; mismo resultado).
Si preguntan costo, ingredientes o cantidades de una subreceta o semi (salsa, masa, relleno), usa costo_subreceta con cod_subreceta o nombre_subreceta; enumera todas las lineas del desglose (MP y SUB hijo) con cantidad, unidad y USD.
Si preguntan que subrecetas hay, catalogo, lista completa o todas las subs (barra y cocina), usa listar_subrecetas y copia texto_whatsapp completo sin resumir.
Si piden revisar platos con costos muy altos, bebidas caras en costo, o MPs mal valorados en recetas, usa auditar_costos_recetas.
Inventario físico / conteo cíclico: para INICIAR un nuevo conteo (crear ciclo + snapshot + hoja Sheets), usa conteo_iniciar con cod_bodega BOD-001 (cocina, hoja CONTEO) o BOD-002 (barra, hoja CONTEO_BARRA). No pidas ejecutar scripts de terminal al usuario. Para ver borradores activos usa conteo_ciclos_abiertos. Tras capturar en Sheets, el envío es menú Conteo → Enviar a Tatami; la aprobación por WA es APROBAR TODO cuando exista sesión de revisión.
Comando directo (sin tool): el usuario puede escribir INICIAR CONTEO BOD-001.
Producción subrecetas (barra/cocina): tool produccion_subreceta o comando PRODUCIR SUB <código> [cantidad] BOD-00x (simula). Cocina (Jacky/staff): BOD-001, subs 002-050 y 055-059 (ej. 006 pan bao, 016 salsa ponzu). Barra (Eduardo): BOD-002, subs 051-054. También por nombre: «preparar pan bao» o «producir salsa ponzu». Para aplicar: CONFIRMAR al final. Inventario del semi = SUB-xxx en BD_MP_SISTEMA por bodega.
No uses markdown, asteriscos ni negritas. Solo texto plano.
Materias primas e inventario: el personal NO conoce codigos MP. Siempre trabaja por NOMBRE del producto. NUNCA inventes cod_mp_sistema ni uses codigos de factura/catalogo. Si hay varios productos parecidos (ej. varios Buchanan), la tool devuelve requiere_eleccion: pregunta al usuario cual es, listando nombres y stock por bodega. No menciones codigos MP al usuario salvo que el mismo los pida.
Traslados: trasladar_mp con nombre_mp (ej. Buchanan 18). Bodegas: cocina, barra, consignacion, externa (o BOD-001/002/003/005; también «005»=externa, «001»=cocina). «de 005 a 001» o «de externa a cocina» son BODEGAS, no códigos de subreceta. Subrecetas (semis SUB-xxx) también se trasladan entre bodegas: «5 tortas de chocolate de cocina a externa» = cantidad en gr/ml según rendimiento × lotes. NO uses produccion_subreceta para trasladar stock ya producido. Si dicen «es subreceta» tras un traslado, es aclaración de tipo, no pedido de producir.
Producción subrecetas — cantidades: rendimiento_estandar = 1 lote (ej. torta chocolate 1054 gr). «Producir una torta» o «6 tortas» → cantidad_lotes=1 o 6 (o cantidad=6 si es entero pequeño); el sistema multiplica por rendimiento. Sin cantidad → un lote estándar.
Stock y PAR: el stock es por bodega; el par_level es global por materia prima (suma stock en todas las bodegas para comparar).
Descargo de ventas solo afecta cocina o barra segun cod_bodega en la receta.
Cuando la fuente sea hist_ventas aclaralo como aproximado.
Nunca inventes ni calcules fechas de memoria: usa el bloque "Contexto temporal" para hoy/ayer y las fechas ISO al llamar tools. Nunca calcules el dia de la semana de una fecha: usa dia_semana y etiqueta_fecha que devuelve ventas_dia."""

_MESES = (
    "",
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def _contexto_fechas_ecuador() -> str:
    """Evita que el modelo alucine 'ayer' (p. ej. 18 de enero) o use UTC."""
    ahora = datetime.now(TZ)
    hoy = ahora.date()
    ayer = hoy - timedelta(days=1)
    dias = (
        "lunes",
        "martes",
        "miercoles",
        "jueves",
        "viernes",
        "sabado",
        "domingo",
    )
    return (
        "Contexto temporal (America/Guayaquil, Ecuador):\n"
        f"- hoy: {hoy.isoformat()} ({dias[hoy.weekday()]} {hoy.day} de {_MESES[hoy.month]} de {hoy.year})\n"
        f"- ayer: {ayer.isoformat()} ({dias[ayer.weekday()]} {ayer.day} de {_MESES[ayer.month]} de {ayer.year})\n"
        "Reglas: si el usuario dice 'ayer', llama ventas_dia con fecha exactamente "
        f'"{ayer.isoformat()}". Si dice "hoy", usa "{hoy.isoformat()}". '
        "No uses otra fecha para ayer/hoy. "
        "Para otras fechas (ej. 30 de mayo), pasa fecha ISO a ventas_dia; "
        "el dia de la semana viene en dia_semana/etiqueta_fecha de la tool, no lo adivines."
    )


historiales = {}


def _asegurar_texto_whatsapp(texto: str | None, *, max_len: int = 3800) -> str:
    """WhatsApp no debe quedar en silencio: respuesta vacía confunde al usuario."""
    s = (texto or "").strip()
    if not s:
        return (
            "No obtuve texto de respuesta. Prueba de nuevo, en una sola línea, "
            "o reformula la pregunta."
        )
    if len(s) > max_len:
        return s[: max_len - 30] + "\n...[mensaje recortado]"
    return s


def _partir_mensajes_whatsapp(texto: str, *, max_len: int = 3800) -> list[str]:
    """
    Divide un texto largo en varios mensajes <= max_len, intentando cortar por líneas.
    Nunca devuelve lista vacía.
    """
    s = (texto or "").strip()
    if not s:
        return [_asegurar_texto_whatsapp("")]
    if len(s) <= max_len:
        return [s]

    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in s.splitlines():
        # Mantener saltos de línea en el cálculo (join agrega \n)
        add = (line + "\n") if line is not None else "\n"
        add_len = len(add)
        if cur and (cur_len + add_len) > max_len:
            chunk = "".join(cur).rstrip()
            if chunk:
                out.append(chunk)
            cur = []
            cur_len = 0

        if add_len > max_len:
            # Línea sola excede max: cortar en bruto
            raw = add
            while raw:
                piece = raw[:max_len]
                out.append(piece.rstrip())
                raw = raw[max_len:]
            continue

        cur.append(add)
        cur_len += add_len

    tail = "".join(cur).rstrip()
    if tail:
        out.append(tail)
    return out or [_asegurar_texto_whatsapp("")]


_SPLIT_MARK = "\n\n---TATAMI_SPLIT---\n\n"


def _system_completo() -> str:
    return SYSTEM + "\n\n" + _contexto_fechas_ecuador()

def llamar_agente(mensaje, telefono):
    try:
        return _llamar_agente_inner(mensaje, telefono)
    except Exception as e:
        print(f"[Agente] llamar_agente fatal telefono={telefono!r}: {e}")
        return (
            "Error al contactar el modelo. "
            f"Detalle técnico: {e!s}. Intenta en unos minutos."
        )


def _llamar_agente_inner(mensaje, telefono):
    if get_rol(telefono) is None:
        return MSG_NO_AUTORIZADO
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    if telefono not in historiales:
        historiales[telefono] = []
    historiales[telefono].append({"role": "user", "content": mensaje})
    messages = list(historiales[telefono])
    max_tool_rounds = 48
    n_round = 0
    while True:
        n_round += 1
        if n_round > max_tool_rounds:
            msg = (
                "Demasiadas herramientas en una sola petición. "
                "Escribe de nuevo la consulta en partes más pequeñas."
            )
            historiales[telefono].append({"role": "assistant", "content": msg})
            if len(historiales[telefono]) > 20:
                historiales[telefono] = historiales[telefono][-20:]
            return msg
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=_system_completo(),
            tools=TOOLS,
            messages=messages,
        )
        texto = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text": texto += block.text
            elif block.type == "tool_use": tool_calls.append(block)
        if response.stop_reason == "end_turn" or not tool_calls:
            out = (texto or "").strip() or _asegurar_texto_whatsapp("")
            historiales[telefono].append({"role": "assistant", "content": out})
            if len(historiales[telefono]) > 20:
                historiales[telefono] = historiales[telefono][-20:]
            return out
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        texto_ventas_directo = ""
        ultimo_user = ""
        for m in reversed(messages):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                ultimo_user = m["content"]
                break
        for tc in tool_calls:
            fn = TOOL_FNS.get(tc.name)
            try:
                if not autorizado_tool(telefono, tc.name):
                    result = {"error": "No autorizado para esta operación."}
                elif (
                    tc.name == "produccion_subreceta"
                    and _parece_intento_traslado(ultimo_user)
                ):
                    result = {
                        "error": (
                            "El usuario pidió TRASLADO de MP entre bodegas, "
                            "no producción de subreceta. Usa trasladar_mp con nombre_mp. "
                            "005/001 en el chat son bodegas (BOD-005/BOD-001), no subs."
                        )
                    }
                else:
                    inp = dict(tc.input or {})
                    if tc.name in ("costo_plato", "receta_ingredientes", "costo_subreceta"):
                        inp["_ocultar_costos"] = not puede_ver_costos(telefono)
                    if tc.name == "produccion_subreceta":
                        inp["_wa_id"] = telefono
                        inp.setdefault("registrado_por", telefono)
                    if tc.name in ("trasladar_mp", "produccion_subreceta") and ultimo_user:
                        inp.setdefault("texto_original", ultimo_user)
                    result = fn(inp) if fn else {"error": f"Tool {tc.name} no encontrada"}
            except Exception as e:
                result = {"error": str(e)}
            if (
                tc.name in (
                    "ventas_por_plato",
                    "ventas_dia",
                    "ventas_por_dia",
                    "compras_facturas_rango",
                    "listar_subrecetas",
                )
                and isinstance(result, dict)
                and (result.get("texto_whatsapp") or "").strip()
            ):
                texto_ventas_directo = (result.get("texto_whatsapp") or "").strip()
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False)
            })
        if texto_ventas_directo and len(tool_calls) == 1:
            partes = _partir_mensajes_whatsapp(texto_ventas_directo, max_len=3800)
            out = _asegurar_texto_whatsapp(partes[0])
            historiales[telefono].append({"role": "assistant", "content": out})
            if len(historiales[telefono]) > 20:
                historiales[telefono] = historiales[telefono][-20:]
            if len(partes) == 1:
                return out
            # Dejar que el handler de WhatsApp envíe en varios mensajes.
            return _SPLIT_MARK.join(partes)
        messages.append({"role": "user", "content": tool_results})


# ── Meta WhatsApp Cloud API (verify + webhook URL típica /webhook) ──
def verificar_firma_meta(payload: bytes, signature: str, app_secret: str) -> bool:
    if not app_secret or not signature:
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.get("/webhook")
async def verificar_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected = (os.getenv("WHATSAPP_VERIFY_TOKEN") or "").strip()
    if mode == "subscribe" and token and expected and token.strip() == expected:
        return PlainTextResponse(str(challenge) if challenge is not None else "")
    return PlainTextResponse("Forbidden", status_code=403)


async def _responder_wa(wa_id: str, texto: str) -> bool:
    """Envía respuesta; loguea si falla la API de Meta."""
    out = _asegurar_texto_whatsapp(texto)
    ok = await enviar_mensaje_meta(wa_id, out)
    if not ok:
        print(f"[Meta] _responder_wa fallo wa_id={wa_id!r} len={len(out)}")
    return ok


async def _enviar_texto_largo_wa(wa_id: str, texto: str) -> bool:
    """Envía texto largo en varios mensajes si hace falta."""
    partes = _partir_mensajes_whatsapp(texto)
    ok = True
    for i, parte in enumerate(partes, 1):
        out = _asegurar_texto_whatsapp(parte)
        if not await enviar_mensaje_meta(wa_id, out):
            print(f"[Meta] _enviar_texto_largo_wa fallo parte {i}/{len(partes)} wa_id={wa_id!r}")
            ok = False
            break
    return ok


async def enviar_typing_meta(telefono: str, message_id: str) -> bool:
    """Marca leído y muestra «escribiendo…» (hasta 25 s o hasta enviar respuesta)."""
    phone_number_id = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not phone_number_id or not token or not (message_id or "").strip():
        return False

    url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id.strip(),
        "typing_indicator": {"type": "text"},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return True
        print(f"[Meta] typing fallo {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        print(f"[Meta] typing error: {e}")
    return False


async def _feedback_procesando(wa_id: str, msg: dict | None = None) -> None:
    """Indicador de escritura si hay message_id; si no, mensaje de espera."""
    message_id = ((msg or {}).get("id") or "").strip()
    if message_id and await enviar_typing_meta(wa_id, message_id):
        return
    await enviar_mensaje_meta(wa_id, MSG_PROCESANDO)


async def enviar_mensaje_meta(telefono: str, texto: str) -> bool:
    phone_number_id = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not phone_number_id or not token:
        print("WARN: falta WHATSAPP_PHONE_NUMBER_ID o WHATSAPP_ACCESS_TOKEN en .env")
        return False

    url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": texto},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    for intento in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, json=payload, headers=headers)
            print(f"[Meta] Enviado a {telefono}: {resp.status_code} (intento {intento})")
            if resp.status_code == 200:
                proc_wa = _wa_procesando_id.get()
                if proc_wa and _norm_tel(proc_wa) == _norm_tel(telefono):
                    _wa_ya_respondio_turno[_norm_tel(telefono)] = True
                return True
            print(f"[Meta] Error body: {resp.text[:500]}")
        except Exception as e:
            err = str(e).strip() or repr(e)
            print(f"[Meta] Error enviando mensaje ({type(e).__name__}): {err} (intento {intento})")
        if intento < 3:
            await asyncio.sleep(1.5 * intento)
    return False


async def enviar_documento_meta(
    telefono: str,
    contenido: bytes,
    nombre_archivo: str,
    mime: str = "application/octet-stream",
) -> bool:
    phone_number_id = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not phone_number_id or not token:
        return False
    try:
        upload_url = f"https://graph.facebook.com/v25.0/{phone_number_id}/media"
        async with httpx.AsyncClient(timeout=30) as client:
            upload_resp = await client.post(
                upload_url,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (nombre_archivo, contenido, mime)},
                data={"messaging_product": "whatsapp"},
            )
        media_id = upload_resp.json().get("id")
        if not media_id:
            print(f"[Meta] No se obtuvo media_id: {upload_resp.text[:300]}")
            return False
        send_url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "document",
            "document": {"id": media_id, "filename": nombre_archivo},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                send_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        return resp.status_code == 200
    except Exception as e:
        print(f"[Meta] Error enviando documento: {e}")
        return False


MENSAJE_PROCESANDO_FACTURA = "Procesando factura... ⏳"


async def encolar_wa_mensaje(wa_id: str, msg: dict) -> None:
    """
    Serializa procesamiento por wa_id: cola + un solo drain activo.
    """
    _wa_pending[wa_id].append(msg)
    if wa_id in _wa_runner_active:
        if wa_id not in _wa_cola_avisado and len(_wa_pending[wa_id]) > 1:
            _wa_cola_avisado.add(wa_id)
            await enviar_mensaje_meta(wa_id, MSG_COLA_ESPERA)
        return
    await _wa_drain(wa_id)


async def _wa_drain(wa_id: str) -> None:
    if wa_id in _wa_runner_active:
        return
    _wa_runner_active.add(wa_id)
    lock = _wa_locks.setdefault(wa_id, asyncio.Lock())
    try:
        async with lock:
            while _wa_pending[wa_id]:
                msg = _wa_pending[wa_id].popleft()
                await procesar_mensaje(wa_id, msg)
    finally:
        _wa_runner_active.discard(wa_id)
        _wa_cola_avisado.discard(wa_id)
        if _wa_pending[wa_id]:
            asyncio.create_task(_wa_drain(wa_id))


async def _wa_runner(wa_id: str) -> None:
    """Compat: delega en _wa_drain."""
    await _wa_drain(wa_id)


async def _manejar_produccion_sub(
    wa_id: str,
    prod_sub: dict,
    msg: dict | None = None,
    *,
    texto: str = "",
) -> None:
    from estrategia_config import bodega_default_produccion_sub, validar_bodega_produccion_sub

    if texto and _es_mensaje_traslado(texto):
        print(f"[Meta] {wa_id}: produccion bloqueada → traslado texto={texto!r}")
        await _manejar_traslado_mp_wa(wa_id, texto, msg)
        return

    if not _autorizado_produccion_sub(wa_id):
        await enviar_mensaje_meta(wa_id, MSG_NO_AUTORIZADO)
        return
    if prod_sub.get("ambiguo") or not prod_sub.get("cods"):
        area = _resolver_area_produccion(
            wa_id,
            "",
            cods=prod_sub.get("cods"),
            area_hint=prod_sub.get("area"),
        )
        ctx = _prod_ctx_get(wa_id)
        if area in ("barra", "cocina"):
            _prod_ctx_touch(wa_id, area=area)
            if ctx.get("catalog_seen"):
                await enviar_mensaje_meta(wa_id, _msg_pedir_nombre_sub(area))
            else:
                await enviar_mensaje_meta(wa_id, _msg_menu_produccion_area(area))
            return
        msg = _msg_batch_no_identificado(wa_id)
        if msg == _msg_batch_preguntar_area():
            _pending_prod_area[wa_id] = "pick"
        await enviar_mensaje_meta(wa_id, msg)
        return
    area = _resolver_area_produccion(wa_id, "", cods=prod_sub.get("cods"))
    _prod_ctx_touch(wa_id, area=area, last_cods=prod_sub.get("cods"))
    _limpiar_ctx_conteo(wa_id)
    bodega = (prod_sub.get("bodega") or bodega_default_produccion_sub(wa_id)).strip().upper()
    err = validar_bodega_produccion_sub(wa_id, bodega)
    if err:
        await enviar_mensaje_meta(wa_id, err)
        return
    prod_sub = {**prod_sub, "bodega": bodega}
    await _feedback_procesando(wa_id, msg)
    try:
        r = await asyncio.to_thread(
            producir_subreceta_wa,
            prod_sub["cods"],
            bodega=prod_sub["bodega"],
            cantidad=prod_sub.get("cantidad"),
            registrado_por="WhatsApp",
            simular=not prod_sub["confirmar"],
            recalcular=prod_sub["confirmar"],
        )
        out = r.get("texto_whatsapp") or str(r)
        if prod_sub["confirmar"]:
            _pending_prod_sub.pop(wa_id, None)
            _pending_prod_area.pop(wa_id, None)
        else:
            _pending_prod_sub[wa_id] = {
                "cods": prod_sub["cods"],
                "bodega": prod_sub["bodega"],
                "cantidad": prod_sub.get("cantidad"),
            }
            if "confirmo" not in out.lower():
                out += (
                    "\n\nPara registrar en inventario responde CONFIRMAR "
                    "o escribe confirmo."
                )
    except SubrecetaOperacionError as e:
        out = f"No se pudo registrar producción: {e.message}"
    except Exception as e:
        out = f"Error: {e}"
    await enviar_mensaje_meta(wa_id, out)


async def procesar_mensaje(wa_id: str, msg: dict) -> None:
    """Procesa un mensaje de Meta en background (POST /webhook ya respondió 200)."""
    wa_key = _norm_tel(wa_id)
    ctx_token = _wa_procesando_id.set(wa_key)
    _wa_ya_respondio_turno.pop(wa_key, None)
    try:
        from wa_chat_guard import touch_wa_chat

        touch_wa_chat(wa_id)
        if get_rol(wa_id) is None:
            await enviar_mensaje_meta(wa_id, MSG_NO_AUTORIZADO)
            return

        mtype = (msg.get("type") or "").strip()

        if mtype == "text":
            texto = _normalizar_texto_comando_wa(
                (msg.get("text", {}).get("body") or "").strip()
            )
            if not texto:
                await _responder_wa(
                    wa_id,
                    "No recibí texto en el mensaje. Escribe tu consulta o, para producir:\n"
                    "Cocina: PRODUCIR SUB 006 BOD-001 · Barra: PRODUCIR SUB 051 BOD-002",
                )
                return
            print(f"[Meta] {wa_id}: {texto} build={TATAMI_WA_BUILD}")

            texto_upper = texto.strip().upper()

            # Traslado: limpiar estado de producción antes de cualquier otra ruta
            texto_traslado = _texto_traslado_combinado(wa_id, texto)
            if _es_mensaje_traslado(texto_traslado) or _es_traslado_implicito(texto_traslado):
                _limpiar_ctx_produccion(wa_id)

            # Aclaración «es subreceta» tras un traslado pendiente
            if await _manejar_aclaracion_traslado(wa_id, texto, msg):
                return

            # Traslados MP / subreceta — PRIMERO (antes de estados pendientes de producción)
            if _es_mensaje_traslado(texto_traslado) or (
                _traslado_ctx_get(wa_id) and _es_traslado_implicito(texto_traslado)
            ):
                print(f"[Meta] {wa_id}: route=traslado")
                await _manejar_traslado_mp_wa(wa_id, texto_traslado, msg)
                return

            # Conteo físico — antes que producción (barra/cocina no son batches)
            if _es_mensaje_conteo(texto, wa_id):
                await _manejar_mensaje_conteo(wa_id, texto)
                return

            # Receta / ingredientes de plato fuerte — sin LLM
            if _es_consulta_receta_plato(texto):
                print(f"[Meta] {wa_id}: route=receta_plato")
                await _manejar_consulta_receta_plato_wa(wa_id, texto, msg)
                return

            # Ventas de hoy — sin LLM
            if _es_consulta_ventas_simple(texto):
                print(f"[Meta] {wa_id}: route=ventas_hoy")
                await _manejar_consulta_ventas_wa(wa_id, msg, texto)
                return

            # Solo "subreceta" — menú de ayuda (no producción ambigua ni LLM)
            if _es_palabra_subreceta_sola(texto):
                print(f"[Meta] {wa_id}: route=ayuda_subreceta")
                await enviar_mensaje_meta(wa_id, MSG_AYUDA_SUBRECETA)
                return

            # Respuesta BARRA / COCINA tras "producir subreceta" sin detalle
            if _pending_prod_area.get(wa_id) == "pick":
                if _es_mensaje_traslado(texto):
                    print(f"[Meta] {wa_id}: route=traslado (cancela pick producción)")
                    await _manejar_traslado_mp_wa(wa_id, texto, msg)
                    return
                area = _parse_area_produccion(texto)
                if area:
                    _prod_ctx_touch(wa_id, area=area)
                    ctx = _prod_ctx_get(wa_id)
                    if ctx.get("catalog_seen"):
                        await enviar_mensaje_meta(wa_id, _msg_pedir_nombre_sub(area))
                    else:
                        await enviar_mensaje_meta(wa_id, _msg_menu_produccion_area(area))
                    return

            # Catálogo de subrecetas (no confundir con producción)
            if _es_consulta_lista_subrecetas(texto):
                _pending_prod_sub.pop(wa_id, None)
                ctx = _prod_ctx_get(wa_id)
                area = _parse_area_produccion(texto) or ctx.get("area")
                _prod_ctx_touch(wa_id, catalog_seen=True, area=area or ctx.get("area"))
                lista = await asyncio.to_thread(_texto_lista_subrecetas_whatsapp, area)
                await _enviar_texto_largo_wa(wa_id, lista)
                return

            # Re-mostrar simulación con nombres de MP (tras pedido explícito)
            if _es_pedido_nombres_mp_produccion(texto) and wa_id in _pending_prod_sub:
                await _manejar_produccion_sub(
                    wa_id,
                    {**_pending_prod_sub[wa_id], "confirmar": False},
                    msg,
                    texto=texto,
                )
                return

            # Batch / producción
            prod_sub = _resolver_prod_sub(texto, wa_id)
            if prod_sub is not None:
                await _manejar_produccion_sub(wa_id, prod_sub, msg, texto=texto)
                return

            # Recordar subreceta mencionada aunque el mensaje vaya al LLM
            cods_men = _match_sub_codigos_en_texto(texto)
            if cods_men:
                ctx0 = _prod_ctx_get(wa_id)
                _prod_ctx_touch(
                    wa_id,
                    last_cods=cods_men,
                    area=_inferir_area_desde_cods(cods_men) or ctx0.get("area"),
                )

            # Comandos de conteo físico (solo si el mensaje ES un comando de conteo)
            sesion_conteo = get_sesion_activa(wa_id)
            if sesion_conteo and _es_comando_conteo(texto_upper):
                if not autorizado_comando(wa_id, texto):
                    await enviar_mensaje_meta(wa_id, MSG_NO_AUTORIZADO)
                    return

                sid = sesion_conteo["id"]
                envio_id = sesion_conteo["envio_id"]

                if texto_upper == "APROBAR TODO":
                    try:
                        aprobar_items(sid, cods=None)
                        sb = conectar_supabase()
                        sb.table("conteo_envio").update(
                            {"estado_aprobacion": "APROBADO_TOTAL"}
                        ).eq("id", envio_id).execute()
                        sb.table("conteo_envio_detalle").update(
                            {"estado_linea": "APROBADO"}
                        ).eq("envio_id", envio_id).eq(
                            "estado_linea", "PENDIENTE_APROBACION"
                        ).execute()
                        res_cont = contabilizar_envio(
                            sb,
                            envio_id,
                            registrado_por=wa_id,
                            cerrar_ciclo=True,
                            recalcular_sheets=True,
                        )
                        cerrar_sesion(sid)
                        numero_moises = (os.getenv("ALERTA_WA_MOISES") or "").strip()
                        numero_felipe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
                        wa_norm = wa_id.lstrip("+").strip()
                        otro = (
                            numero_felipe
                            if wa_norm == numero_moises.lstrip("+")
                            else numero_moises
                        )
                        if otro:
                            sesion_otro = get_sesion_activa(otro)
                            if sesion_otro:
                                cerrar_sesion(sesion_otro["id"])
                        out = (
                            "Conteo aprobado y contabilizado.\n"
                            f"Movimientos insertados: {res_cont['movimientos_insertados']}\n"
                            f"Sin ajuste (delta < umbral): {res_cont['saltadas_umbral']}"
                        )
                        if res_cont.get("advertencias"):
                            out += "\nAvisos: " + "; ".join(res_cont["advertencias"])
                    except Exception as e:
                        out = f"Error al contabilizar: {e}"
                    await enviar_mensaje_meta(wa_id, out)
                    return

                if texto_upper.startswith("APROBAR "):
                    nombre = texto[8:].strip()
                    resultado = aprobar_items(sid, cods=[nombre])
                    if resultado["aprobados"]:
                        pendientes = len(resultado["pendientes"])
                        out = (
                            f"Aprobado: {nombre}.\nPendientes: {pendientes} ítems.\n"
                            "Responde APROBAR TODO cuando termines."
                        )
                    else:
                        out = (
                            f"No encontré '{nombre}' en los pendientes. "
                            "Verifica el nombre exacto."
                        )
                    await enviar_mensaje_meta(wa_id, out)
                    return

                if texto_upper.startswith("RECHAZAR "):
                    nombre = texto[9:].strip()
                    resultado = rechazar_items(sid, cods=[nombre])
                    if resultado["rechazados"]:
                        pendientes = len(resultado["pendientes"])
                        out = f"Rechazado: {nombre}.\nPendientes: {pendientes} ítems."
                    else:
                        out = f"No encontré '{nombre}' en los pendientes."
                    await enviar_mensaje_meta(wa_id, out)
                    return

                if texto_upper.startswith("KARDEX "):
                    nombre = texto[7:].strip()
                    deltas = json.loads(sesion_conteo["deltas_pendientes"])
                    delta_item = next(
                        (d for d in deltas if nombre.upper() in d["nombre_mp"].upper()),
                        None,
                    )
                    if not delta_item:
                        await enviar_mensaje_meta(
                            wa_id,
                            f"No encontré '{nombre}' en las diferencias del conteo.",
                        )
                        return
                    fecha_hasta = date.today().isoformat()
                    fecha_desde = (date.today() - timedelta(days=30)).isoformat()
                    try:
                        kardex = get_kardex(
                            delta_item["cod_mp_sistema"], fecha_desde, fecha_hasta
                        )
                        texto_kardex = formatear_kardex_wa(
                            kardex,
                            stock_snapshot=delta_item["stock_snapshot"],
                            conteo_fisico=delta_item["conteo_fisico"],
                            costo_ref=delta_item.get("costo_ref"),
                        )
                    except Exception as e:
                        texto_kardex = f"Error generando kardex: {e}"
                    await enviar_mensaje_meta(wa_id, texto_kardex)
                    return

                if texto_upper.startswith("CSV "):
                    nombre = texto[4:].strip()
                    deltas = json.loads(sesion_conteo["deltas_pendientes"])
                    delta_item = next(
                        (d for d in deltas if nombre.upper() in d["nombre_mp"].upper()),
                        None,
                    )
                    if not delta_item:
                        await enviar_mensaje_meta(
                            wa_id,
                            f"No encontré '{nombre}' en las diferencias del conteo.",
                        )
                        return
                    fecha_hasta = date.today().isoformat()
                    fecha_desde = (date.today() - timedelta(days=30)).isoformat()
                    try:
                        kardex = get_kardex(
                            delta_item["cod_mp_sistema"], fecha_desde, fecha_hasta
                        )
                        xlsx_bytes = generar_xlsx(kardex)
                        nombre_archivo = (
                            f"kardex_{delta_item['cod_mp_sistema']}_{fecha_desde}.xlsx"
                        )
                        await enviar_documento_meta(
                            wa_id,
                            xlsx_bytes,
                            nombre_archivo,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    except Exception as e:
                        await enviar_mensaje_meta(wa_id, f"Error generando Excel: {e}")
                    return

            # Comando rápido: INICIAR CONTEO BOD-001
            cod_bod_rapido = _parse_iniciar_conteo_comando(texto)
            if cod_bod_rapido is not None:
                if not autorizado_comando(wa_id, texto):
                    await enviar_mensaje_meta(wa_id, MSG_NO_AUTORIZADO)
                    return
                if not cod_bod_rapido:
                    ay, aw = semana_iso_actual()
                    out = (
                        "Uso: INICIAR CONTEO BOD-001\n"
                        "o INICIAR CONTEO BOD-002 (barra).\n"
                        f"Semana actual: W{aw} {ay}."
                    )
                else:
                    try:
                        r = iniciar_conteo_wa(cod_bod_rapido, sobreescribir_hoja=True)
                        out = (
                            f"Conteo iniciado — {r['cod_bodega']} W{r['semana_iso']}/{r['anio']}\n"
                            f"ciclo_id: {r['ciclo_id']}\n"
                            f"Hoja: {r['sheet_name']} ({r['mps_en_snapshot']} MPs)\n"
                            f"URL: {r.get('url_hoja', '')}\n\n"
                            "Pasos:\n"
                            + "\n".join(f"• {x}" for x in r.get("instrucciones", []))
                        )
                    except ConteoOperacionError as e:
                        out = f"No se pudo iniciar conteo: {e.message}"
                    except Exception as e:
                        out = f"Error: {e}"
                await enviar_mensaje_meta(wa_id, out)
                return

            # Sesión de factura activa
            if hay_sesion_activa(wa_id):
                conf = parse_confirmacion_factura(texto)
                if conf.get("action") in ("cancel", "apply"):
                    try:
                        resp = await handle_confirmacion(texto, wa_id)
                    except Exception as e:
                        print(f"[Meta] handle_confirmacion: {e}")
                        resp = (
                            f"Error al confirmar: {e!s}. Reenvía el archivo o escribe CANCELAR."
                        )
                    out = _asegurar_texto_whatsapp(resp)
                    await enviar_mensaje_meta(wa_id, out)
                    return

            # Agente general — nunca LLM si el mensaje es traslado
            if _parece_intento_traslado(texto):
                print(f"[Meta] {wa_id}: route=traslado (fallback pre-LLM)")
                await _manejar_traslado_mp_wa(wa_id, texto, msg)
                return

            print(f"[Meta] {wa_id}: route=llm")
            await _feedback_procesando(wa_id, msg)
            try:
                respuesta = await asyncio.to_thread(llamar_agente, texto, wa_id)
            except Exception as e:
                print(f"[Meta] llamar_agente: {e}")
                respuesta = (
                    "Error al contactar el modelo. "
                    f"Detalle técnico: {e!s}. Intenta en unos minutos."
                )
            if isinstance(respuesta, str) and _SPLIT_MARK in respuesta:
                partes = [p.strip() for p in respuesta.split(_SPLIT_MARK) if p.strip()]
                if not partes:
                    partes = [_asegurar_texto_whatsapp("")]
                for i, p in enumerate(partes, 1):
                    out = _asegurar_texto_whatsapp(p)
                    ok_send = await enviar_mensaje_meta(wa_id, out)
                    if not ok_send:
                        print(f"[Meta] enviar_mensaje_meta fallo (parte {i}/{len(partes)}) wa_id={wa_id!r}")
                        break
            else:
                out = _asegurar_texto_whatsapp(respuesta)
                ok_send = await enviar_mensaje_meta(wa_id, out)
                if not ok_send:
                    print(f"[Meta] enviar_mensaje_meta fallo para wa_id={wa_id!r}")
            return

        if mtype in ("image", "document"):
            await enviar_mensaje_meta(wa_id, MENSAJE_PROCESANDO_FACTURA)
            try:
                respuesta = await handle_mensaje_media(msg, wa_id)
            except Exception as e:
                print(f"[Meta] handle_mensaje_media: {e}")
                respuesta = f"No pude procesar el archivo: {e!s}"
            out = _asegurar_texto_whatsapp(respuesta)
            ok_send = await enviar_mensaje_meta(wa_id, out)
            if not ok_send:
                print(f"[Meta] enviar_mensaje_meta fallo (media) wa_id={wa_id!r}")
            return

        await _responder_wa(wa_id, MSG_TIPO_NO_SOPORTADO)
    except Exception as e:
        import traceback

        print(f"[Meta] procesar_mensaje wa_id={wa_id!r}: {e}")
        print(traceback.format_exc())
        if not _wa_ya_respondio_turno.get(wa_key):
            try:
                await _responder_wa(wa_id, MSG_ERROR_PROCESO)
            except Exception as e2:
                print(f"[Meta] no se pudo enviar fallback de error: {e2}")
        else:
            print(f"[Meta] error tras respuesta enviada wa_id={wa_id!r}; sin MSG_ERROR_PROCESO")
    finally:
        _wa_procesando_id.reset(ctx_token)
        _wa_ya_respondio_turno.pop(wa_key, None)


async def _lanzar_procesamiento_wa(wa_id: str, msg: dict) -> None:
    try:
        await encolar_wa_mensaje(wa_id, msg)
    except Exception as e:
        print(f"[Meta] _lanzar_procesamiento_wa wa_id={wa_id!r}: {e}")
        _log_webhook_event(f"ERROR procesando {wa_id}: {e}")
        try:
            await _responder_wa(wa_id, MSG_ERROR_PROCESO)
        except Exception:
            pass


@app.post("/webhook")
async def recibir_webhook_meta(request: Request):
    body = await request.body()
    skip_sig = (os.getenv("WHATSAPP_SKIP_SIGNATURE") or "").strip() == "1"
    if not skip_sig:
        app_secret = (os.getenv("WHATSAPP_APP_SECRET") or "").strip()
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not app_secret:
            print("[Meta webhook] 401: falta WHATSAPP_APP_SECRET en .env (guardar y reiniciar)")
            raise HTTPException(status_code=401, detail="Firma webhook inválida")
        if not signature:
            print("[Meta webhook] 401: falta header X-Hub-Signature-256")
            raise HTTPException(status_code=401, detail="Firma webhook inválida")
        if not verificar_firma_meta(body, signature, app_secret):
            print("[Meta webhook] 401: firma no coincide — revisar WHATSAPP_APP_SECRET en Meta Developer")
            raise HTTPException(status_code=401, detail="Firma webhook inválida")
    try:
        data = json.loads(body)
    except Exception:
        return {"status": "ok"}

    print(f"[Meta webhook] {json.dumps(data, ensure_ascii=False)[:300]}")
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                for msg in value.get("messages", []) or []:
                    msg_id = (msg.get("id") or "").strip()
                    wa_from = (msg.get("from") or "").strip()
                    mtype = (msg.get("type") or "").strip()
                    preview = ""
                    if mtype == "text":
                        preview = ((msg.get("text") or {}).get("body") or "")[:120]
                    _log_webhook_event(
                        f"IN from={wa_from} type={mtype} id={msg_id} build={TATAMI_WA_BUILD} body={preview!r}"
                    )
                    if msg_id and mensaje_ya_procesado(msg_id):
                        continue
                    wa_id = wa_from
                    if not wa_id:
                        continue
                    asyncio.create_task(_lanzar_procesamiento_wa(wa_id, msg))
                for st in value.get("statuses", []) or []:
                    if (st.get("status") or "") != "failed":
                        continue
                    err = ((st.get("errors") or [{}])[0].get("error_data") or {}).get(
                        "details", ""
                    )
                    _log_webhook_event(
                        f"OUT FAILED to={st.get('recipient_id')} err={err[:200]}"
                    )
    except Exception as e:
        print(f"[Meta webhook] Error: {e}")
    return {"status": "ok"}


@app.post("/whatsapp")
async def webhook(Body: str = Form(...), From: str = Form(...)):
    telefono = (From or "").strip()
    body = (Body or "").strip()
    print(f"[{telefono}] {body}")
    lock = _wa_locks.setdefault(telefono, asyncio.Lock())
    async with lock:
        try:
            respuesta = llamar_agente(body, telefono)
        except Exception as e:
            respuesta = f"Error interno: {str(e)}"
    print(f"[Agente] {respuesta}")
    twiml = MessagingResponse()
    twiml.message(_asegurar_texto_whatsapp(respuesta))
    return PlainTextResponse(str(twiml), media_type="application/xml")

@app.get("/")
def health():
    try:
        from google_credentials import has_google_credentials

        tools_n = len(TOOLS)
    except Exception as e:
        return {
            "status": "ok",
            "wa_build": TATAMI_WA_BUILD,
            "tools_error": str(e),
        }
    return {
        "status": "ok",
        "agente": "Tatami Bao Bar v4",
        "tools": tools_n,
        "wa_build": TATAMI_WA_BUILD,
        "git_commit": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:12],
        "anthropic_key_set": bool((os.getenv("ANTHROPIC_API_KEY") or "").strip()),
        "google_creds_set": has_google_credentials(),
    }

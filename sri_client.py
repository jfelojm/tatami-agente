"""
Cliente SRI Ecuador: listado portal (Playwright) + descarga XML (SOAP zeep).

Portal: comprobantes recibidos → claves de acceso (49 dígitos).
SOAP: AutorizacionComprobantesOffline.autorizacionComprobante(clave).
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from dotenv import load_dotenv

load_dotenv(override=True)

CLAVE_ACCESO_RE = re.compile(r"\b(\d{49})\b")
SRI_PORTAL_URLS = (
    "https://srienlinea.sri.gob.ec/comprobantes-electronicos-internet/pages/consultas/consultaComprobantesRecibidos.jsf",
    "https://srienlinea.sri.gob.ec/comprobantes-electronicos-internet/pages/consultas/recuperarComprobantesRecibidos.jsf",
    "https://srienlinea.sri.gob.ec/sri-en-linea/consulta/55",
)
SRI_MENU_URL = "https://srienlinea.sri.gob.ec/sri-en-linea/consulta/55"
MESES_ES = (
    "",
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)
SRI_PORTAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Oculta navigator.webdriver sin flags CLI (Chrome advierte --disable-blink-features).
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""

WSDL_AUTORIZACION = {
    "produccion": (
        "https://cel.sri.gob.ec/comprobantes-electronicos-ws/"
        "AutorizacionComprobantesOffline?wsdl"
    ),
    "pruebas": (
        "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/"
        "AutorizacionComprobantesOffline?wsdl"
    ),
}


@dataclass
class SriConfig:
    ruc: str
    portal_user: str
    portal_password: str
    cert_p12_path: str
    cert_password: str
    ventana_dias: int = 7
    ambiente: str = "produccion"
    portal_storage_state: str = ""
    portal_headless: bool = True
    portal_browser: str = "chrome"
    captcha_timeout_sec: int = 180
    consulta_modo: str = "auto"
    captcha_solver_key: str = ""
    recaptcha_enterprise: bool = True
    consulta_retries: int = 3
    descarga_modo: str = "portal"

    @classmethod
    def from_env(cls) -> "SriConfig":
        amb = (os.getenv("SRI_AMBIENTE") or "produccion").strip().lower()
        if amb not in WSDL_AUTORIZACION:
            amb = "produccion"
        storage = (os.getenv("SRI_PORTAL_STORAGE_STATE") or "").strip()
        if not storage:
            storage = str(
                Path(__file__).resolve().parent / "credentials" / "sri_portal_state.json"
            )
        headless = (os.getenv("SRI_PORTAL_HEADLESS") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        try:
            ventana = int(os.getenv("SRI_VENTANA_DIAS") or "7")
        except ValueError:
            ventana = 7
        try:
            captcha_to = int(os.getenv("SRI_CAPTCHA_TIMEOUT_SEC") or "90")
        except ValueError:
            captcha_to = 90
        browser = (os.getenv("SRI_PORTAL_BROWSER") or "chrome").strip().lower()
        if browser not in ("chrome", "chromium", "msedge"):
            browser = "chrome"
        modo = (os.getenv("SRI_CONSULTA_MODO") or "auto").strip().lower()
        if modo not in ("auto", "manual", "solver"):
            modo = "auto"
        try:
            reintentos = int(os.getenv("SRI_CONSULTA_REINTENTOS") or "3")
        except ValueError:
            reintentos = 3
        solver_key = (os.getenv("SRI_CAPTCHA_2CAPTCHA_KEY") or "").strip()
        recaptcha_ent = (os.getenv("SRI_RECAPTCHA_ENTERPRISE") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        if solver_key and modo == "auto":
            modo = "solver"
        descarga = (os.getenv("SRI_DESCARGA_MODO") or "portal").strip().lower()
        if descarga not in ("portal", "soap", "auto"):
            descarga = "portal"
        return cls(
            ruc=(os.getenv("SRI_RUC") or "").strip(),
            portal_user=(os.getenv("SRI_PORTAL_USER") or "").strip(),
            portal_password=(os.getenv("SRI_PORTAL_PASSWORD") or "").strip(),
            cert_p12_path=(os.getenv("SRI_CERT_P12_PATH") or "").strip(),
            cert_password=(os.getenv("SRI_CERT_PASSWORD") or "").strip(),
            ventana_dias=max(1, min(ventana, 30)),
            ambiente=amb,
            portal_storage_state=storage,
            portal_headless=headless,
            portal_browser=browser,
            captcha_timeout_sec=max(30, captcha_to),
            consulta_modo=modo,
            captcha_solver_key=solver_key,
            recaptcha_enterprise=recaptcha_ent,
            consulta_retries=max(1, min(reintentos, 6)),
            descarga_modo=descarga,
        )

    def sesion_portal_guardada(self) -> bool:
        profile = Path(self.portal_storage_state).parent / "sri_chrome_profile"
        if (profile / "Default").is_dir():
            return True
        return Path(self.portal_storage_state).is_file()

    def profile_dir(self) -> Path:
        p = Path(self.portal_storage_state).parent / "sri_chrome_profile"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def validar(self) -> list[str]:
        faltantes = []
        if not self.ruc:
            faltantes.append("SRI_RUC")
        if not self.portal_user:
            faltantes.append("SRI_PORTAL_USER")
        if not self.portal_password:
            faltantes.append("SRI_PORTAL_PASSWORD")
        return faltantes


@dataclass
class ComprobanteRecibido:
    clave_acceso: str
    num_factura: str = ""
    ruc_emisor: str = ""
    razon_social: str = ""
    fecha_emision: str = ""
    tipo_comprobante: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def ventana_fechas(dias: int, hasta: date | None = None) -> tuple[date, date]:
    """
    Ventana inclusive: hoy + `dias` días calendario previos.
    Ej. hoy 11-jun y dias=7 -> 4-jun .. 11-jun (7 previos + hoy).
    """
    fin = hasta or date.today()
    inicio = fin - timedelta(days=max(1, dias))
    return inicio, fin


def _fecha_desde_clave(clave: str) -> date | None:
    """DDMMYYYY en posiciones 1-8 de la clave de acceso SRI."""
    c = re.sub(r"\D", "", clave or "")
    if len(c) < 8:
        return None
    try:
        return date(int(c[4:8]), int(c[2:4]), int(c[0:2]))
    except ValueError:
        return None


def _fecha_comprobante(comp: ComprobanteRecibido) -> date | None:
    fe = (comp.fecha_emision or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(fe[:10], fmt).date()
        except ValueError:
            continue
    return _fecha_desde_clave(comp.clave_acceso)


def filtrar_comprobantes_ventana(
    items: list[ComprobanteRecibido],
    fecha_desde: date,
    fecha_hasta: date,
) -> list[ComprobanteRecibido]:
    """Conserva comprobantes cuya fecha de emisión cae en [desde, hasta]."""
    out: list[ComprobanteRecibido] = []
    for comp in items:
        fd = _fecha_comprobante(comp)
        if fd is None:
            continue
        if fecha_desde <= fd <= fecha_hasta:
            out.append(comp)
    return out


def _filas_coinciden_dia(filas: list[dict[str, Any]], fecha: date) -> bool:
    """True si todas las filas con clave corresponden al día consultado."""
    claves = [str(f.get("clave") or "").strip() for f in filas if f.get("clave")]
    if not claves:
        return False
    for clave in claves:
        if _fecha_desde_clave(clave) != fecha:
            return False
    return True


def _normalizar_clave(clave: str) -> str:
    c = re.sub(r"\D", "", (clave or "").strip())
    if len(c) != 49:
        raise ValueError(f"clave_acceso inválida ({len(c)} dígitos, se esperan 49)")
    return c


def _obj_a_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_obj_a_dict(x) for x in obj]
    if hasattr(obj, "__dict__") and not isinstance(obj, (str, bytes, int, float, bool)):
        try:
            from zeep.helpers import serialize_object

            return serialize_object(obj)
        except Exception:
            pass
    if isinstance(obj, dict):
        return {k: _obj_a_dict(v) for k, v in obj.items()}
    return obj


def _extraer_comprobante_xml(respuesta: Any) -> str:
    data = _obj_a_dict(respuesta)
    if not data:
        raise RuntimeError("Respuesta SOAP vacía del SRI")

    autorizaciones = None
    if isinstance(data, dict):
        autorizaciones = data.get("autorizaciones")
        if autorizaciones is None and "autorizacion" in data:
            autorizaciones = {"autorizacion": data.get("autorizacion")}

    if not autorizaciones:
        raise RuntimeError(f"Sin autorizaciones en respuesta SRI: {data!r}")

    items = autorizaciones.get("autorizacion")
    if items is None:
        raise RuntimeError(f"Estructura autorizaciones inesperada: {autorizaciones!r}")
    if not isinstance(items, list):
        items = [items]

    for aut in items:
        if not isinstance(aut, dict):
            continue
        estado = str(aut.get("estado") or "").upper()
        if estado and estado not in ("AUTORIZADO", "AUTORIZADA"):
            msg = aut.get("mensajes") or aut.get("informacionAdicional") or estado
            raise RuntimeError(f"Comprobante no autorizado ({estado}): {msg}")
        comp = aut.get("comprobante")
        if comp:
            return str(comp).strip()

    raise RuntimeError("No se encontró nodo comprobante en respuesta SOAP")


def _wrap_xml_autorizado(clave: str, comprobante_inner: str) -> str:
    """Envuelve el XML interno en estructura autorización que entiende parsear_xml_sri."""
    inner = comprobante_inner.strip()
    if inner.startswith("<?xml"):
        inner = inner.split("?>", 1)[-1].strip()
    if "<autorizacion" in inner.lower() and "<comprobante" in inner.lower():
        return inner
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<autorizacion>"
        f"<estado>AUTORIZADO</estado>"
        f"<numeroAutorizacion>{clave}</numeroAutorizacion>"
        f"<fechaAutorizacion>{datetime.now().strftime('%Y-%m-%dT%H:%M:%S-05:00')}</fechaAutorizacion>"
        f"<comprobante><![CDATA[{inner}]]></comprobante>"
        "</autorizacion>"
    )


def _normalizar_xml_descargado(clave: str, raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("XML vacio del portal SRI")
    if not text.lstrip().startswith("<"):
        raise RuntimeError("Respuesta portal no es XML")
    low = text.lower()
    if "<autorizacion" in low and "<comprobante" in low:
        return text
    if any(tag in low for tag in ("<factura", "<notacredito", "<notadebito", "<liquidacion")):
        return _wrap_xml_autorizado(clave, text)
    raise RuntimeError("XML descargado no reconocido como comprobante SRI")


_JS_FILAS_TABLA = """
() => {
    const rows = Array.from(document.querySelectorAll(
        '.ui-datatable-data tr, table.ui-datatable-data tbody tr'
    ));
    return rows.map((tr, idx) => {
        const txt = (tr.innerText || '').replace(/\\s+/g, ' ').trim();
        const claveM = txt.match(/\\b(\\d{49})\\b/);
        const factM = txt.match(/\\b(\\d{3}-\\d{3}-\\d{9})\\b/);
        const rucM = txt.match(/\\b(\\d{13})\\b/);
        const xmlLink = tr.querySelector(
            'a[id*="lnkXml"], a[id*="lnkDocumento"], a[onclick*="lnkXml"]'
        );
        let razon = '';
        const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText||'').trim());
        for (const c of cells) {
            if (c.length > 8 && !/^\\d+$/.test(c) && !c.includes('-') && !claveM) continue;
            if (c.length > 8 && !/^\\d+$/.test(c) && !/\\d{3}-\\d{3}-\\d{9}/.test(c)) {
                razon = c;
                break;
            }
        }
        return {
            idx,
            clave: claveM ? claveM[1] : '',
            num_factura: factM ? factM[1] : '',
            ruc_emisor: rucM ? rucM[1] : '',
            razon_social: razon,
            xml_link_id: xmlLink ? (xmlLink.id || '') : '',
            has_xml_link: !!xmlLink,
        };
    }).filter(r => r.clave || r.has_xml_link);
}
"""


class SriSoapClient:
    """Descarga XML autorizado por clave de acceso (WS público, sin p12)."""

    def __init__(self, config: SriConfig):
        self.config = config
        self._client = None

    def _cliente(self):
        if self._client is None:
            try:
                from requests import Session
                from requests.adapters import HTTPAdapter
                from urllib3.util.retry import Retry
                from zeep import Client
                from zeep.transports import Transport
            except ImportError as e:
                raise RuntimeError(
                    "Falta dependencia zeep. Ejecute: pip install zeep"
                ) from e
            wsdl = WSDL_AUTORIZACION[self.config.ambiente]
            host = (
                "https://cel.sri.gob.ec"
                if self.config.ambiente == "produccion"
                else "https://celcer.sri.gob.ec"
            )
            service_url = (
                f"{host}/comprobantes-electronicos-ws/AutorizacionComprobantesOffline"
            )
            session = Session()
            retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[502, 503, 504])
            session.mount("https://", HTTPAdapter(max_retries=retry))
            transport = Transport(session=session, timeout=60, operation_timeout=90)
            client = Client(wsdl, transport=transport)
            client.service._binding_options["address"] = service_url
            self._client = client
        return self._client

    def descargar_xml_autorizado(self, clave_acceso: str) -> str:
        clave = _normalizar_clave(clave_acceso)
        last_err = None
        for intento in range(1, 4):
            try:
                client = self._cliente()
                resp = client.service.autorizacionComprobante(
                    claveAccesoComprobante=clave
                )
                inner = _extraer_comprobante_xml(resp)
                return _wrap_xml_autorizado(clave, inner)
            except Exception as e:
                last_err = e
                if intento < 3:
                    time.sleep(2 * intento)
                    self._client = None
        raise RuntimeError(f"SOAP SRI fallo para clave {clave[:12]}...: {last_err}")


def _parsear_reporte_txt(contenido: str) -> list[ComprobanteRecibido]:
    """Extrae comprobantes del reporte TXT del portal (claves de 49 dígitos)."""
    vistos: set[str] = set()
    out: list[ComprobanteRecibido] = []
    for linea in contenido.splitlines():
        claves = CLAVE_ACCESO_RE.findall(linea)
        if not claves:
            continue
        for clave in claves:
            if clave in vistos:
                continue
            vistos.add(clave)
            campos = re.split(r"\t+", linea.strip())
            comp = ComprobanteRecibido(clave_acceso=clave)
            for tok in campos:
                t = tok.strip()
                if re.fullmatch(r"\d{13}", t):
                    comp.ruc_emisor = t
                elif re.fullmatch(r"\d{3}-\d{3}-\d{9}", t):
                    comp.num_factura = t
                elif re.match(r"\d{2}/\d{2}/\d{4}", t):
                    comp.fecha_emision = t
                elif len(t) > 5 and not re.fullmatch(r"\d+", t):
                    if not comp.razon_social:
                        comp.razon_social = t
            out.append(comp)
    return out


def _parsear_html_tabla(html: str) -> list[ComprobanteRecibido]:
    vistos: set[str] = set()
    out: list[ComprobanteRecibido] = []
    for clave in CLAVE_ACCESO_RE.findall(html):
        if clave in vistos:
            continue
        vistos.add(clave)
        out.append(ComprobanteRecibido(clave_acceso=clave))
    return out


def _meses_en_rango(desde: date, hasta: date) -> list[tuple[int, int]]:
    """Lista (anio, mes_num) cubriendo el rango inclusive."""
    out: list[tuple[int, int]] = []
    y, m = desde.year, desde.month
    while (y, m) <= (hasta.year, hasta.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _dias_en_rango(desde: date, hasta: date) -> list[date]:
    """Cada dia calendario en [desde, hasta]. Portal SRI consulta un dia a la vez."""
    out: list[date] = []
    d = desde
    while d <= hasta:
        out.append(d)
        d += timedelta(days=1)
    return out


def _dedup_comprobantes(items: list[ComprobanteRecibido]) -> list[ComprobanteRecibido]:
    vistos: set[str] = set()
    out: list[ComprobanteRecibido] = []
    for c in items:
        if c.clave_acceso in vistos:
            continue
        vistos.add(c.clave_acceso)
        out.append(c)
    return out


class SriPortalClient:
    """Lista comprobantes recibidos vía portal SRI en línea (Playwright)."""

    def __init__(self, config: SriConfig):
        self.config = config

    def _chrome_launch_flags(self, *, headless: bool) -> tuple[list[str], list[str]]:
        """Sin --disable-blink-features=AutomationControlled (banner Chrome + consultas vacias SRI)."""
        ignore_default = ["--enable-automation"]
        args: list[str] = []
        if not headless:
            ignore_default.append("--no-sandbox")
            args.extend(
                [
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--new-window",
                    "--start-maximized",
                ]
            )
        return args, ignore_default

    @staticmethod
    def _apply_stealth(ctx) -> None:
        try:
            ctx.add_init_script(_STEALTH_INIT_SCRIPT)
        except Exception:
            pass

    def _try_launch_persistent_with_channel(self, p, profile_dir: Path, *, headless: bool):
        """
        Abre navegador con perfil persistente usando Chrome/Edge del sistema.
        Importante: NO usar Chromium embebido (puede ser bloqueado por 'Validación de navegadores').
        """
        launch_args, ignore_default = self._chrome_launch_flags(headless=headless)
        base_kwargs: dict[str, Any] = {
            "locale": "es-EC",
            "timezone_id": "America/Guayaquil",
            "accept_downloads": True,
            "user_agent": SRI_PORTAL_USER_AGENT,
            "viewport": {"width": 1366, "height": 900},
            "headless": headless,
            "args": launch_args,
            "ignore_default_args": ignore_default,
        }

        preferred = (self.config.portal_browser or "chrome").strip().lower()
        # Edge suele pasar mejor la validacion de navegador del SRI en Windows.
        channels = []
        if preferred == "msedge":
            channels = ["msedge", "chrome"]
        elif preferred == "chrome":
            channels = ["chrome", "msedge"]
        else:
            channels = [preferred]
        channels = [c for c in channels if c in ("chrome", "msedge")]

        last_err: Exception | None = None
        for ch in channels:
            try:
                kwargs = dict(base_kwargs)
                kwargs["channel"] = ch
                ctx = p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
                self._apply_stealth(ctx)
                print(
                    f"Navegador: {ch} (perfil {profile_dir.name}, "
                    f"{'headless' if headless else 'visible'})"
                )
                return ctx
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(
            "No se pudo abrir Chrome/Edge del sistema para SRI.\n"
            "El portal puede bloquear Chromium embebido (Validación de navegadores).\n"
            f"Detalle: {last_err}"
        )

    def _avisos_portal(self, page, work) -> list[str]:
        """Mensajes visibles (banner Chrome, reCAPTCHA, SRI sin resultados)."""
        avisos: list[str] = []
        keys = (
            "marca de línea de comandos",
            "marca de linea de comandos",
            "command-line flag",
            "no admitida",
            "recaptcha",
            "no se encontr",
            "sin registro",
            "no existen comprobante",
            "no tiene comprobante",
            "intente nuevamente",
            "error en la consulta",
            "navegador no es soportada",
            "versión de su navegador",
            "version de su navegador",
            "espere por favor",
        )
        for target in (page, work):
            try:
                texto = (target.inner_text("body") or "")[:5000]
            except Exception:
                continue
            for line in texto.splitlines():
                l = line.strip()
                if not l or len(l) < 12:
                    continue
                low = l.lower()
                if any(k in low for k in keys) and l not in avisos:
                    avisos.append(l)
        return avisos[:10]

    def _reportar_consulta_vacia(self, page, work, fecha: date) -> None:
        avisos = self._avisos_portal(page, work)
        if avisos:
            print(f"    AVISOS en pagina ({fecha.isoformat()}):")
            for a in avisos:
                print(f"      · {a[:220]}")
        else:
            print(
                f"    Sin filas ese dia (o tabla no renderizada). "
                f"Pruebe el mismo dia en Chrome normal (no Playwright)."
            )

    def _context_kwargs(self, storage_path: Path | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "locale": "es-EC",
            "timezone_id": "America/Guayaquil",
            "accept_downloads": True,
            "user_agent": SRI_PORTAL_USER_AGENT,
            "viewport": {"width": 1366, "height": 900},
        }
        if storage_path and storage_path.is_file():
            kwargs["storage_state"] = str(storage_path)
        return kwargs

    def _playwright(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Falta playwright. Ejecute: pip install playwright"
            ) from e
        return sync_playwright

    def _launch_persistent(self, p, *, headless: bool):
        """Siempre abre Chrome con perfil persistente (crea carpeta si no existe)."""
        profile = self.config.profile_dir()
        try:
            ctx = self._try_launch_persistent_with_channel(p, profile, headless=headless)
        except Exception as e:
            err = str(e).lower()
            if "existing" in err or "has been closed" in err or "user data directory" in err:
                raise RuntimeError(
                    "Chrome del SRI ya esta abierto (perfil en uso).\n"
                    "Cierre TODAS las ventanas de Chrome que abrio --init-portal-session,\n"
                    "presione ENTER en PowerShell para guardar sesion, y vuelva a lanzar la descarga."
                ) from e
            raise
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if not headless:
            try:
                page.bring_to_front()
            except Exception:
                pass
        ctx._tatami_mode = "persistent"  # type: ignore[attr-defined]
        return ctx, page

    def _open_context(self, p, *, headless: bool):
        """Corridas automaticas: perfil persistente, o storage state legacy."""
        profile = self.config.profile_dir()
        if (profile / "Default").is_dir():
            return self._launch_persistent(p, headless=headless)

        state = Path(self.config.portal_storage_state)
        if state.is_file():
            print("  Perfil Chrome vacio; usando storage state legacy.")
            print("  Renueve sesion: .\\ejecutar_facturas_sri.ps1 --init-portal-session")
            kwargs: dict[str, Any] = {
                "locale": "es-EC",
                "timezone_id": "America/Guayaquil",
                "accept_downloads": True,
                "user_agent": SRI_PORTAL_USER_AGENT,
                "viewport": {"width": 1366, "height": 900},
            }
            browser_name = self.config.portal_browser
            launch_args, ignore_default = self._chrome_launch_flags(headless=headless)
            launch_kw: dict[str, Any] = {
                "headless": headless,
                "args": launch_args,
                "ignore_default_args": ignore_default,
            }
            if browser_name in ("chrome", "msedge"):
                launch_kw["channel"] = browser_name
            try:
                browser = p.chromium.launch(**launch_kw)
            except Exception as e:
                if browser_name in ("chrome", "msedge"):
                    raise RuntimeError(
                        f"No se pudo abrir {browser_name} para SRI. Detalle: {e}"
                    ) from e
                browser = p.chromium.launch(**launch_kw)
            ctx = browser.new_context(**kwargs, storage_state=str(state))
            self._apply_stealth(ctx)
            page = ctx.new_page()
            ctx._tatami_mode = "browser"  # type: ignore[attr-defined]
            ctx._tatami_browser = browser  # type: ignore[attr-defined]
            print(f"Navegador: {browser_name} (storage state)")
            return ctx, page

        raise RuntimeError(
            "Sin sesion SRI. Ejecute: .\\ejecutar_facturas_sri.ps1 --init-portal-session"
        )

    @staticmethod
    def _close_context(ctx) -> None:
        mode = getattr(ctx, "_tatami_mode", "persistent")
        if mode == "browser":
            browser = getattr(ctx, "_tatami_browser", None)
            ctx.close()
            if browser:
                browser.close()
        else:
            ctx.close()

    def _launch_browser(self, p, *, headless: bool):
        """Usa Google Chrome instalado por defecto (channel=chrome)."""
        browser_name = self.config.portal_browser
        launch_args, ignore_default = self._chrome_launch_flags(headless=headless)
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "args": launch_args,
            "ignore_default_args": ignore_default,
        }
        if browser_name in ("chrome", "msedge"):
            launch_kwargs["channel"] = browser_name

        try:
            browser = p.chromium.launch(**launch_kwargs)
            print(f"Navegador: {browser_name}")
            return browser
        except Exception as e:
            if browser_name in ("chrome", "msedge"):
                raise RuntimeError(
                    f"No se pudo abrir {browser_name}. Instale Chrome o use Edge. Detalle: {e}"
                ) from e
            return p.chromium.launch(**launch_kwargs)

    def _pagina_trabajo(self, page):
        """Devuelve frame con el formulario de consulta (iframe o pagina principal)."""
        work = self._buscar_work_formulario(page)
        if work is not None:
            return work
        for frame in page.frames:
            url = (frame.url or "").lower()
            if (
                "comprobantes-electronicos" in url
                or "comprobantesrecibidos" in url
                or "consulta" in url
            ):
                return frame
        return page

    def _click_menu_comprobantes_recibidos(self, page) -> bool:
        return bool(
            page.evaluate(
                """() => {
                    const el = [...document.querySelectorAll('span.ui-menuitem-text')]
                        .find(s => /comprobantes electr.*recibidos/i.test(s.textContent || ''));
                    if (!el) return false;
                    (el.closest('a') || el).click();
                    return true;
                }"""
            )
        )

    def _navegar_a_formulario_recibidos(self, page):
        """Tuportal Angular -> menu -> JSF comprobantes (2 logins Keycloak posibles)."""
        print(f"  Navegando menu SRI: {SRI_MENU_URL}")
        page.goto(SRI_MENU_URL, wait_until="commit", timeout=120_000)
        for _ in range(45):
            if self._pagina_en_carga_sri(page):
                page.wait_for_timeout(2000)
                continue
            break
        page.wait_for_timeout(2000)

        if self._necesita_login(page):
            print("  Login Keycloak (tuportal)...")
            self._login_si_necesario(page)

        if not self._click_menu_comprobantes_recibidos(page):
            raise RuntimeError(
                "No se encontro el menu 'Comprobantes electronicos recibidos' en SRI en linea."
            )
        page.wait_for_timeout(4000)

        if self._necesita_login(page):
            print("  Login Keycloak (modulo comprobantes)...")
            self._login_si_necesario(page)

        for _ in range(30):
            work = self._buscar_work_formulario(page) or self._pagina_trabajo(page)
            if self._en_formulario_consulta(work):
                return work
            if self._pagina_en_carga_sri(page):
                page.wait_for_timeout(2000)
                continue
            page.wait_for_timeout(2000)

        raise RuntimeError(
            "Timeout esperando formulario de comprobantes recibidos en portal SRI."
        )

    def _navegar_formulario_manual(self, page):
        """Modo manual: usuario inicia sesión y abre el menú en Chrome."""
        print(f"  Abriendo portal SRI en Chrome: {SRI_MENU_URL}")
        page.goto(SRI_MENU_URL, wait_until="commit", timeout=120_000)
        page.wait_for_timeout(2000)
        try:
            page.bring_to_front()
        except Exception:
            pass
        print("")
        print("  === MODO MANUAL (Chrome) ===")
        print("    1. Inicie sesión si lo pide (RUC + clave + captcha)")
        print("    2. Menú → Facturación electrónica → Comprobantes electrónicos recibidos")
        print("    3. Confirme selects AÑO / MES / DÍA / TIPO visibles")
        print("")
        work = self._esperar_formulario_consulta(page, timeout_sec=60)
        if work is not None:
            return work
        try:
            input("  Presione ENTER cuando vea el formulario de comprobantes recibidos... ")
        except EOFError:
            print("  Esperando 3 min para que abra el formulario...")
            page.wait_for_timeout(180_000)
        work = self._esperar_formulario_consulta(page, timeout_sec=90)
        if work is None:
            self._guardar_debug(page, "manual_sin_formulario")
            raise RuntimeError(
                "No se detectó formulario SRI.\n"
                "Deje visible 'Comprobantes electrónicos recibidos' con botón CONSULTAR\n"
                "y vuelva a ejecutar: .\\configurar_sri_chrome.ps1 -Descargar"
            )
        print(f"  OK: formulario detectado ({page.url or 'portal SRI'})")
        return work

    def _goto_portal(self, page) -> Any:
        last_err = None
        for url in SRI_PORTAL_URLS:
            try:
                print(f"  Navegando: {url}")
                page.goto(url, wait_until="commit", timeout=90_000)
                page.wait_for_timeout(3000)
                body = page.inner_text("body")[:500].lower()
                if "error" in body and "404" in body:
                    continue
                return self._pagina_trabajo(page)
            except Exception as e:
                last_err = e
                print(f"  WARN goto: {e}")
        raise RuntimeError(f"No se pudo abrir portal SRI: {last_err}")

    def _necesita_login(self, page) -> bool:
        url = (page.url or "").lower()
        if "auth/realms" in url or "/login" in url:
            return True
        try:
            if page.locator('input[type="password"]:visible').count() > 0:
                return True
        except Exception:
            pass
        return False

    def _pagina_en_carga_sri(self, page) -> bool:
        try:
            txt = (page.inner_text("body") or "")[:800].lower()
            if "espere por favor" in txt:
                return page.locator("select").count() < 2
        except Exception:
            pass
        return False

    def _texto_indica_formulario_recibidos(self, texto: str) -> bool:
        low = (texto or "").lower()
        return (
            "comprobantes electr" in low
            and "recibidos" in low
            and ("periodo emisi" in low or "ingrese los datos para la consulta" in low)
        )

    def _frame_tiene_formulario_recibidos(self, frame) -> bool:
        try:
            if frame.locator('[id="frmPrincipal:ano"], [id="frmPrincipal:mes"]').count():
                return True
            if frame.locator(
                'button:has-text("Consultar"), [id="frmPrincipal:btnBuscar"]'
            ).count():
                txt = (frame.inner_text("body") or "")[:4000]
                if self._texto_indica_formulario_recibidos(txt):
                    return True
            if frame.locator("select").count() >= 2:
                txt = (frame.inner_text("body") or "")[:2000]
                return self._texto_indica_formulario_recibidos(txt)
        except Exception:
            pass
        return False

    def _buscar_work_formulario(self, page):
        """Frame o pagina con formulario comprobantes recibidos (portal SRI 2025+)."""
        candidatos = [page, *page.frames]
        for frame in candidatos:
            try:
                if self._frame_tiene_formulario_recibidos(frame):
                    return frame
            except Exception:
                continue
        return None

    def _esperar_formulario_consulta(self, page, *, timeout_sec: int = 90):
        """Espera formulario en la pagina actual sin navegar (respeta donde quedo el usuario)."""
        import time

        limite = time.time() + timeout_sec
        while time.time() < limite:
            work = self._buscar_work_formulario(page)
            if work is not None:
                return work
            if self._texto_indica_formulario_recibidos(page.inner_text("body") or ""):
                return page
            if self._necesita_login(page):
                page.wait_for_timeout(2000)
            elif self._pagina_en_carga_sri(page):
                page.wait_for_timeout(2000)
            else:
                page.wait_for_timeout(1500)
        work = self._buscar_work_formulario(page)
        if work is not None:
            return work
        if self._texto_indica_formulario_recibidos(page.inner_text("body") or ""):
            return page
        return None

    def _en_formulario_consulta(self, work) -> bool:
        return self._frame_tiene_formulario_recibidos(work)

    def _guardar_debug(self, page, etiqueta: str) -> None:
        log_dir = Path(__file__).resolve().parent / "logs" / "sri_debug"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = log_dir / f"{etiqueta}_{ts}"
        try:
            page.screenshot(path=str(base) + ".png", full_page=True)
            Path(str(base) + ".html").write_text(page.content(), encoding="utf-8")
            Path(str(base) + ".txt").write_text(
                page.inner_text("body")[:8000], encoding="utf-8"
            )
            print(f"  Debug guardado: {base}.png")
        except Exception as e:
            print(f"  WARN debug: {e}")

    def guardar_sesion_interactiva(self) -> Path:
        """Abre Chrome (perfil persistente) para login manual; cookies quedan en disco."""
        dest = Path(self.config.portal_storage_state)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._playwright()() as p:
            ctx, page = self._launch_persistent(p, headless=False)
            print("Abriendo portal SRI en Chrome...")
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.goto(SRI_MENU_URL, wait_until="commit", timeout=120_000)
            page.wait_for_timeout(3000)
            try:
                page.bring_to_front()
                page.evaluate("window.focus()")
            except Exception:
                pass
            print("Si no ve la ventana: barra de tareas (Chrome) o Alt+Tab.")
            print("")
            print("=" * 60)
            print("INSTRUCCIONES (no cierre Chrome todavia):")
            print("  1. Si pide login: RUC + clave (+ captcha si aparece)")
            print("  2. En el menu izquierdo abra Facturacion electronica")
            print("  3. Click en 'Comprobantes electronicos recibidos'")
            print("  4. Si pide login otra vez, repita credenciales")
            print("  5. Confirme que ve selects de ANIO / MES / TIPO")
            print("  6. Si sale error de reCAPTCHA: F5 (recargar) o cierre y reabra el script")
            print("  7. Si la cabecera dice 'marca de linea de comandos no admitida',")
            print("     cierre Chrome, actualice el agente y vuelva a ejecutar init")
            print("  8. Con el formulario visible, vuelva a PowerShell y presione ENTER")
            print("     (NO cierre Chrome; el script NO volvera a navegar)")
            print("=" * 60)
            print("")
            try:
                input("Presione ENTER cuando vea el formulario de comprobantes recibidos... ")
            except EOFError:
                print("WARN: stdin no disponible; esperando 180s para login manual...")
                page.wait_for_timeout(180_000)

            print("Verificando formulario en la pagina actual (sin navegar)...")
            work = self._esperar_formulario_consulta(page, timeout_sec=45)
            if work is None and self._necesita_login(page):
                self._close_context(ctx)
                raise RuntimeError(
                    "Sesion NO guardada: sigue en pantalla de login.\n"
                    "Ejecute de nuevo e inicie sesion antes de presionar ENTER."
                )
            if work is None:
                print(f"  URL al presionar ENTER: {page.url}")
                avisos = self._avisos_portal(page, self._pagina_trabajo(page))
                if avisos:
                    print("  Avisos detectados:")
                    for a in avisos:
                        print(f"    · {a[:220]}")
                self._guardar_debug(page, "init_sin_formulario")
                self._close_context(ctx)
                raise RuntimeError(
                    "Sesion NO guardada: no se detecto formulario de comprobantes recibidos.\n"
                    "Deje abierta la pantalla con selects ANIO/MES/DIA/TIPO y presione ENTER.\n"
                    "Si ve 'Espere por favor' o 'navegador no soportado', espere a que cargue o use Edge."
                )
            print("  OK: formulario de comprobantes detectado.")
            self._close_context(ctx)
        print(f"Sesion guardada en perfil: {self.config.profile_dir()}")
        return self.config.profile_dir()

    def listar_recibidos(
        self,
        fecha_desde: date,
        fecha_hasta: date,
        tipo_comprobante: str = "Factura",
        *,
        descargar_xml: bool | None = None,
    ) -> list[ComprobanteRecibido]:
        if descargar_xml is None:
            # auto: solo lista claves en portal; XML vía SOAP en fase_descarga
            descargar_xml = self.config.descarga_modo == "portal"
        if not self.config.sesion_portal_guardada():
            raise RuntimeError(
                "No hay sesion del portal SRI.\n"
                "Ejecute primero: .\\ejecutar_facturas_sri.ps1 --init-portal-session"
            )
        if self.config.consulta_modo == "manual" and self.config.portal_headless:
            raise RuntimeError(
                "SRI_CONSULTA_MODO=manual requiere SRI_PORTAL_HEADLESS=0 en .env"
            )

        with self._playwright()() as p:
            ctx, page = self._open_context(p, headless=self.config.portal_headless)

            if self.config.consulta_modo == "manual":
                work = self._navegar_formulario_manual(page)
            else:
                work = self._navegar_a_formulario_recibidos(page)
            print("  Formulario de consulta listo.")
            print(
                f"  Modo consulta: {self.config.consulta_modo} | "
                f"navegador={'headless' if self.config.portal_headless else 'visible'}"
            )
            if self.config.consulta_modo == "manual":
                print("  Usted hace clic en CONSULTAR por cada día.")
            if descargar_xml:
                print("  Descarga XML: portal SRI (por fila, dialogo Archivo XML).")

            comprobantes: list[ComprobanteRecibido] = []
            dias = _dias_en_rango(fecha_desde, fecha_hasta)
            usar_mes = len(dias) > 14
            # Consulta mensual solo si la ventana arranca al inicio del mes; si no,
            # la 1ª página del SRI suele ser facturas viejas y se pierden dias 14+.
            if (
                not usar_mes
                and len(dias) > 1
                and fecha_desde.year == fecha_hasta.year
                and fecha_desde.month == fecha_hasta.month
                and fecha_desde.day <= 7
            ):
                usar_mes = True
                print(
                    f"  Rango {fecha_desde}..{fecha_hasta} al inicio del mes: "
                    "consulta mensual (menos captchas) y filtro por ventana."
                )
            if usar_mes:
                meses = _meses_en_rango(fecha_desde, fecha_hasta)
                print(
                    f"  Consultando {len(meses)} mes(es) con dia=Todos "
                    f"(mas fiable que dia a dia en rangos largos)..."
                )
                for anio, mes in meses:
                    print(
                        f"  Mes {anio}-{mes:02d} "
                        f"({MESES_ES[mes]} {anio}, todos los dias)"
                    )
                    self._consultar_por_mes(
                        work,
                        anio,
                        mes,
                        tipo_comprobante=tipo_comprobante,
                        root_page=page,
                    )
                    found = self._obtener_resultados(
                        work, page, descargar_xml=descargar_xml
                    )
                    print(f"    -> {len(found)} comprobante(s)")
                    if not found:
                        self._reportar_consulta_vacia(
                            page, work, date(anio, mes, 1)
                        )
                    comprobantes.extend(found)
            else:
                print(f"  Consultando {len(dias)} dia(s) (uno por consulta SRI)...")
                for fecha in dias:
                    print(
                        f"  Dia {fecha.isoformat()} "
                        f"({MESES_ES[fecha.month]} {fecha.day}, {fecha.year})"
                    )
                    self._consultar_por_dia(
                        work,
                        fecha,
                        tipo_comprobante=tipo_comprobante,
                        root_page=page,
                    )
                    found = self._obtener_resultados_dia(
                        work,
                        page,
                        fecha,
                        tipo_comprobante=tipo_comprobante,
                        descargar_xml=descargar_xml,
                    )
                    print(f"    -> {len(found)} comprobante(s)")
                    if not found:
                        self._reportar_consulta_vacia(page, work, fecha)
                    comprobantes.extend(found)

            comprobantes = _dedup_comprobantes(comprobantes)
            antes = len(comprobantes)
            comprobantes = filtrar_comprobantes_ventana(
                comprobantes, fecha_desde, fecha_hasta
            )
            if antes != len(comprobantes):
                print(
                    f"  Ventana {fecha_desde} .. {fecha_hasta}: "
                    f"{len(comprobantes)} de {antes} comprobante(s)"
                )
            if not comprobantes:
                self._guardar_debug(page, "sin_resultados")

            self._close_context(ctx)
            return comprobantes

    def _login_si_necesario(self, page) -> None:
        if not self._necesita_login(page):
            return
        user = self.config.portal_user
        pwd = self.config.portal_password
        if not user or not pwd:
            raise RuntimeError("Faltan SRI_PORTAL_USER / SRI_PORTAL_PASSWORD en .env")

        # Keycloak SRI en linea (form id kc-form-login)
        try:
            u = page.locator("#usuario, input[name='usuario']").first
            if u.count() and u.is_visible(timeout=5000):
                u.fill(user)
                page.locator("#password, input[name='password']").first.fill(pwd)
                btn = page.locator("#kc-login, input[name='login']").first
                with page.expect_navigation(timeout=90_000, wait_until="commit"):
                    btn.click()
                page.wait_for_timeout(4000)
                try:
                    page.wait_for_load_state("networkidle", timeout=45_000)
                except Exception:
                    pass
                if not self._necesita_login(page):
                    print("  Login Keycloak OK.")
                    return
        except Exception as e:
            print(f"  WARN login Keycloak: {e}")

        if self._pagina_pide_captcha_login(page):
            if self.config.portal_headless:
                raise RuntimeError(
                    "SRI pide captcha en login. Ejecute:\n"
                    "  .\\ejecutar_facturas_sri.ps1 --init-portal-session\n"
                    "O use: $env:SRI_PORTAL_HEADLESS='0'; .\\ejecutar_facturas_sri.ps1 --corrida MANUAL"
                )
            print(f"Resuelva captcha de login ({self.config.captcha_timeout_sec}s)...")
            try:
                page.bring_to_front()
            except Exception:
                pass
            self._esperar_captcha_resuelto(page, page, self.config.captcha_timeout_sec)
            return

        # Fallback generico
        posibles_user = [
            "#usuario",
            'input[name="usuario"]',
            'input[name="ruc"]',
        ]
        posibles_pass = ['#password', 'input[name="password"]', 'input[type="password"]']

        for sel in posibles_user:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=2000):
                    loc.fill(user)
                    break
            except Exception:
                continue

        for sel in posibles_pass:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=2000):
                    loc.fill(pwd)
                    break
            except Exception:
                continue

        for sel in (
            "#kc-login",
            'input[name="login"]',
            'input[value="Ingresar"]',
            'button:has-text("Ingresar")',
        ):
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=2000):
                    btn.click()
                    break
            except Exception:
                continue

        page.wait_for_timeout(5000)

    def _paginas_a_revisar(self, root_page, work=None):
        vistos: set[int] = set()
        orden = []
        for p in (root_page, work):
            if p is None:
                continue
            pid = id(p)
            if pid not in vistos:
                vistos.add(pid)
                orden.append(p)
        try:
            for f in root_page.frames:
                pid = id(f)
                if pid not in vistos:
                    vistos.add(pid)
                    orden.append(f)
        except Exception:
            pass
        return orden

    def _recaptcha_challenge_visible(self, page) -> bool:
        for sel in (
            'iframe[src*="recaptcha/api2/bframe"]',
            'iframe[title*="recaptcha challenge" i]',
            'iframe[title*="desafío de recaptcha" i]',
        ):
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 3)):
                    if loc.nth(i).is_visible(timeout=400):
                        return True
            except Exception:
                continue
        return False

    def _pagina_pide_captcha_login(self, page) -> bool:
        if self._recaptcha_challenge_visible(page):
            return True
        try:
            if self._necesita_login(page):
                anchor = page.locator('iframe[src*="recaptcha/api2/anchor"]')
                if anchor.count() and anchor.first.is_visible(timeout=500):
                    return True
        except Exception:
            pass
        return False

    def _error_captcha_consulta(self, root_page, work) -> bool:
        """Banner amarillo 'Captcha incorrecta' (reCAPTCHA invisible fallo)."""
        for p in self._paginas_a_revisar(root_page, work):
            try:
                for sel in (
                    ".ui-messages-warn-detail",
                    ".ui-messages-warn-summary",
                    ".ui-messages-error-detail",
                    ".ui-messages-error-summary",
                    ".ui-messages-warn",
                    ".ui-messages-error",
                ):
                    loc = p.locator(sel)
                    for i in range(min(loc.count(), 5)):
                        t = (loc.nth(i).inner_text() or "").lower()
                        if "captcha" in t:
                            return True
                txt = (p.inner_text("body") or "")[:5000].lower()
                if "captcha incorrect" in txt:
                    return True
            except Exception:
                continue
        return False

    def _pagina_pide_captcha_consulta(self, root_page, work) -> bool:
        """Desafio visible o error de captcha tras Consultar."""
        if self._error_captcha_consulta(root_page, work):
            return True
        if self._recaptcha_challenge_visible(root_page) or self._recaptcha_challenge_visible(work):
            return True
        for p in self._paginas_a_revisar(root_page, work):
            try:
                txt = (p.inner_text("body") or "")[:4000].lower()
            except Exception:
                continue
            if not any(
                k in txt
                for k in (
                    "resuelva el captcha",
                    "verifique que no es un robot",
                    "complete el captcha",
                    "error de validacion captcha",
                )
            ):
                continue
            if self._recaptcha_challenge_visible(p):
                return True
            try:
                dlg = p.locator(".ui-dialog-content:visible, .ui-messages-error:visible")
                if dlg.count():
                    dt = (dlg.first.inner_text() or "").lower()
                    if "captcha" in dt or "robot" in dt:
                        return True
            except Exception:
                pass
        return False

    def _texto_zona_resultados(self, work, root_page) -> str:
        partes: list[str] = []
        for target in (work, root_page):
            for sel in (
                ".ui-datatable",
                ".ui-messages",
                "[id*='tabla' i]",
                "[id*='resultado' i]",
                "[class*='datatable' i]",
            ):
                try:
                    loc = target.locator(sel)
                    if loc.count():
                        partes.append((loc.first.inner_text(timeout=800) or "").strip())
                except Exception:
                    pass
        return "\n".join(p for p in partes if p)

    def _tiene_filas_comprobantes(self, work, root_page) -> bool:
        for target in (work, root_page):
            try:
                for sel in (
                    ".ui-datatable-data tr",
                    "table.ui-datatable-data tbody tr",
                    "table tbody tr",
                ):
                    loc = target.locator(sel)
                    n = min(loc.count(), 30)
                    for i in range(n):
                        row_txt = (loc.nth(i).inner_text(timeout=500) or "").strip()
                        if CLAVE_ACCESO_RE.search(row_txt):
                            return True
                        if len(row_txt) > 20 and re.search(r"\d{3}-\d{3}-\d{9}", row_txt):
                            return True
            except Exception:
                pass
        return False

    def _mensaje_sin_registros(self, work, root_page) -> bool:
        vacio_msgs = (
            "no se encontr",
            "sin registro",
            "no existen comprobante",
            "no existen datos",
            "no tiene comprobante",
            "no hay comprobantes",
        )
        for target in (work, root_page):
            try:
                txt = self._texto_zona_resultados(work, root_page).lower()
                if not txt:
                    txt = (target.inner_text("body") or "").lower()
                if any(m in txt for m in vacio_msgs):
                    return True
            except Exception:
                pass
        return False

    def _tiene_resultado_consulta(self, work, root_page) -> bool:
        if self._error_captcha_consulta(root_page, work):
            return False
        if self._tiene_filas_comprobantes(work, root_page):
            return True
        return self._mensaje_sin_registros(work, root_page)

    def _esperar_consulta_manual(self, root_page, work, fecha: date, *, timeout_sec: int) -> str:
        """Navegador visible: el usuario hace click en Consultar (captcha invisible SRI)."""
        print("")
        print(f"  >>> Día {fecha.isoformat()}: haga clic en CONSULTAR en Chrome <<<")
        print("  (Captcha invisible: el script NO hace clic automático.)")
        print("  Si no ve el botón CONSULTAR, suba con la rueda del mouse o pulse F5.")
        try:
            root_page.bring_to_front()
        except Exception:
            pass
        root_page.wait_for_timeout(800)
        baseline = self._texto_zona_resultados(work, root_page)
        aviso_captcha = False
        limite = time.time() + timeout_sec
        while time.time() < limite:
            if self._error_captcha_consulta(root_page, work):
                if not aviso_captcha:
                    print(
                        "  SRI: 'Captcha incorrecta' — recargue (F5) y vuelva a clic en CONSULTAR."
                    )
                    aviso_captcha = True
            elif aviso_captcha:
                aviso_captcha = False
            if self._tiene_filas_comprobantes(work, root_page):
                print("  Consulta OK (comprobantes en tabla).")
                return "ok"
            actual = self._texto_zona_resultados(work, root_page)
            if actual != baseline and self._mensaje_sin_registros(work, root_page):
                print("  Consulta OK (sin registros ese dia).")
                return "ok"
            if self._recaptcha_challenge_visible(root_page) or self._recaptcha_challenge_visible(work):
                print("  Resuelva el captcha visible en Chrome...")
            root_page.wait_for_timeout(1500)
        return "timeout"

    def _esperar_post_consulta(self, root_page, work, *, timeout_sec: int = 60) -> str:
        limite = time.time() + timeout_sec
        while time.time() < limite:
            if self._error_captcha_consulta(root_page, work):
                return "captcha"
            if self._tiene_resultado_consulta(work, root_page):
                return "ok"
            if self._pagina_pide_captcha_consulta(root_page, work):
                return "captcha"
            root_page.wait_for_timeout(1000)
        if self._error_captcha_consulta(root_page, work):
            return "captcha"
        if self._tiene_resultado_consulta(work, root_page):
            return "ok"
        if self._pagina_pide_captcha_consulta(root_page, work):
            return "captcha"
        return "timeout"

    def _esperar_post_consulta_dia(
        self,
        root_page,
        work,
        fecha: date,
        *,
        timeout_sec: int = 60,
    ) -> str:
        """Espera resultados del día consultado (rechaza tabla obsoleta de otro día)."""
        limite = time.time() + timeout_sec
        while time.time() < limite:
            if self._error_captcha_consulta(root_page, work):
                return "captcha"
            if self._pagina_pide_captcha_consulta(root_page, work):
                return "captcha"
            if self._mensaje_sin_registros(work, root_page):
                return "ok"
            filas = self._parsear_filas_tabla(work)
            if filas and _filas_coinciden_dia(filas, fecha):
                return "ok"
            root_page.wait_for_timeout(1000)
        if self._error_captcha_consulta(root_page, work):
            return "captcha"
        if self._mensaje_sin_registros(work, root_page):
            return "ok"
        filas = self._parsear_filas_tabla(work)
        if filas and _filas_coinciden_dia(filas, fecha):
            return "ok"
        if filas and not _filas_coinciden_dia(filas, fecha):
            return "stale"
        if self._pagina_pide_captcha_consulta(root_page, work):
            return "captcha"
        return "timeout"

    def _esperar_captcha_resuelto(self, root_page, work, timeout_sec: int) -> None:
        print(f"  Esperando captcha ({timeout_sec}s max)...")
        try:
            root_page.bring_to_front()
        except Exception:
            pass
        limite = time.time() + timeout_sec
        while time.time() < limite:
            if not self._pagina_pide_captcha_consulta(root_page, work):
                if self._tiene_resultado_consulta(work, root_page):
                    print("  Captcha resuelto / consulta cargada.")
                    return
            root_page.wait_for_timeout(1500)
        raise RuntimeError(
            "Timeout esperando captcha en consulta SRI.\n"
            "Ejecute con navegador visible y resuelva el captcha a tiempo:\n"
            "  $env:SRI_PORTAL_HEADLESS='0'\n"
            "  .\\ejecutar_facturas_sri.ps1 --corrida MANUAL"
        )

    def _pagina_pide_captcha(self, page) -> bool:
        """Compat: login u hoja unica."""
        return self._pagina_pide_captcha_login(page)

    def _extraer_recaptcha_params(self, work) -> dict[str, str | None]:
        try:
            return work.evaluate(
                """() => {
                    const out = { sitekey: null, data_s: null, action: null };
                    const el = document.querySelector('[data-sitekey]');
                    if (el) {
                        out.sitekey = el.getAttribute('data-sitekey');
                        out.data_s = el.getAttribute('data-s');
                    }
                    if (!out.data_s) {
                        const ds = document.querySelector('[data-s]');
                        if (ds) out.data_s = ds.getAttribute('data-s');
                    }
                    const scripts = Array.from(document.scripts)
                        .map(s => s.textContent || '').join('\\n');
                    const mS = scripts.match(/["']s["']\\s*:\\s*["']([^"']{20,})["']/);
                    if (!out.data_s && mS) out.data_s = mS[1];
                    if (!out.sitekey) {
                        for (const fr of document.querySelectorAll('iframe[src*="recaptcha"]')) {
                            const m = (fr.src || '').match(/[?&]k=([^&]+)/);
                            if (m) { out.sitekey = decodeURIComponent(m[1]); break; }
                        }
                    }
                    const mA = scripts.match(/action\\s*:\\s*["']([^"']+)["']/);
                    if (mA) out.action = mA[1];
                    return out;
                }"""
            )
        except Exception:
            return {"sitekey": None, "data_s": None, "action": None}

    def _extraer_recaptcha_sitekey(self, work) -> str | None:
        return self._extraer_recaptcha_params(work).get("sitekey")

    def _debe_usar_2captcha(self) -> bool:
        return bool(self.config.captcha_solver_key) and self.config.consulta_modo == "solver"

    def _aplicar_captcha_antes_consultar(self, work, root_page) -> None:
        if not self._debe_usar_2captcha():
            return
        self._aplicar_recaptcha_solver(work, root_page)

    def _inyectar_recaptcha_token(self, work, token: str) -> None:
        work.evaluate(
            """(token) => {
                document.querySelectorAll(
                    'textarea[name="g-recaptcha-response"], [name="g-recaptcha-response"]'
                ).forEach(el => {
                    el.value = token;
                    el.innerHTML = token;
                });
                try {
                    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
                        const clients = window.___grecaptcha_cfg.clients;
                        for (const id of Object.keys(clients)) {
                            const c = clients[id];
                            const cb = c && (c.callback || (c.G && c.G.callback));
                            if (typeof cb === 'function') {
                                cb(token);
                            }
                        }
                    }
                } catch (e) {}
            }""",
            token,
        )

    def _resolver_recaptcha_2captcha(
        self,
        page_url: str,
        sitekey: str,
        *,
        invisible: bool = True,
        data_s: str | None = None,
    ) -> str:
        api_key = self.config.captcha_solver_key
        if not api_key:
            raise RuntimeError("Falta SRI_CAPTCHA_2CAPTCHA_KEY en .env")

        page_url = (page_url or SRI_MENU_URL).split("#")[0]
        timeout_sec = self.config.captcha_timeout_sec
        last_err = "2captcha: sin respuesta"

        def _api_json_legacy(base: str, params: dict[str, str]) -> dict:
            params = {**params, "key": api_key, "json": "1"}
            if base.endswith("in.php"):
                data = urllib.parse.urlencode(params).encode()
                req = urllib.request.Request(base, data=data, method="POST")
            else:
                url = base + "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode())

        def _poll_legacy(task_id: str) -> str:
            nonlocal last_err
            limite = time.time() + timeout_sec
            while time.time() < limite:
                time.sleep(5)
                poll = _api_json_legacy(
                    "https://2captcha.com/res.php",
                    {"action": "get", "id": task_id},
                )
                if poll.get("status") == 1:
                    return str(poll["request"])
                req_msg = str(poll.get("request") or "")
                last_err = f"2captcha res.php: {req_msg}"
                if req_msg != "CAPCHA_NOT_READY":
                    raise RuntimeError(last_err)
            raise RuntimeError("2captcha: timeout esperando token")

        def _solve_v2() -> str:
            nonlocal last_err
            task: dict[str, Any] = {
                "type": "RecaptchaV2EnterpriseTaskProxyless"
                if self.config.recaptcha_enterprise
                else "RecaptchaV2TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "isInvisible": invisible,
                "userAgent": SRI_PORTAL_USER_AGENT,
            }
            if data_s:
                task["enterprisePayload"] = {"s": data_s}
            body = json.dumps({"clientKey": api_key, "task": task}).encode()
            req = urllib.request.Request(
                "https://api.2captcha.com/createTask",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                created = json.loads(resp.read().decode())
            if created.get("errorId"):
                last_err = (
                    f"2captcha createTask: {created.get('errorCode')} "
                    f"{created.get('errorDescription', '')}".strip()
                )
                raise RuntimeError(last_err)
            task_id = created["taskId"]
            limite = time.time() + timeout_sec
            while time.time() < limite:
                time.sleep(5)
                poll_body = json.dumps(
                    {"clientKey": api_key, "taskId": task_id}
                ).encode()
                poll_req = urllib.request.Request(
                    "https://api.2captcha.com/getTaskResult",
                    data=poll_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(poll_req, timeout=90) as resp:
                    poll = json.loads(resp.read().decode())
                if poll.get("errorId") and poll.get("status") != "processing":
                    last_err = (
                        f"2captcha getTaskResult: {poll.get('errorCode')} "
                        f"{poll.get('errorDescription', '')}".strip()
                    )
                    raise RuntimeError(last_err)
                if poll.get("status") == "ready":
                    token = (poll.get("solution") or {}).get("gRecaptchaResponse")
                    if token:
                        return str(token)
                    last_err = "2captcha: respuesta sin gRecaptchaResponse"
                    raise RuntimeError(last_err)
            raise RuntimeError("2captcha: timeout esperando token (API v2)")

        def _solve_legacy() -> str:
            nonlocal last_err
            payload: dict[str, str] = {
                "method": "userrecaptcha",
                "googlekey": sitekey,
                "pageurl": page_url,
            }
            if invisible:
                payload["invisible"] = "1"
            if self.config.recaptcha_enterprise:
                payload["enterprise"] = "1"
            if data_s:
                payload["data-s"] = data_s
            created = _api_json_legacy("https://2captcha.com/in.php", payload)
            if created.get("status") != 1:
                last_err = f"2captcha in.php: {created.get('request')}"
                raise RuntimeError(last_err)
            return _poll_legacy(str(created["request"]))

        for solver in (_solve_v2, _solve_legacy):
            try:
                return solver()
            except RuntimeError as e:
                last_err = str(e)
                if "UNSOLVABLE" in last_err.upper():
                    continue
                raise
        raise RuntimeError(last_err)

    def _aplicar_recaptcha_solver(self, work, root_page) -> bool:
        params = self._extraer_recaptcha_params(work)
        sitekey = params.get("sitekey")
        if not sitekey:
            print("  AVISO: no se encontro sitekey reCAPTCHA; click directo en Consultar.")
            return False
        page_url = (work.url or root_page.url or SRI_MENU_URL).split("#")[0]
        data_s = params.get("data_s")
        print(
            f"  Resolviendo reCAPTCHA (2captcha, sitekey {sitekey[:16]}..., "
            f"data-s={'si' if data_s else 'no'})..."
        )
        try:
            token = self._resolver_recaptcha_2captcha(
                page_url,
                sitekey,
                invisible=True,
                data_s=data_s,
            )
        except Exception as e:
            print(f"  AVISO 2captcha: {e}")
            print("  Continuando con click directo (navegador real)...")
            return False
        self._inyectar_recaptcha_token(work, token)
        work.wait_for_timeout(500)
        return True

    def _click_consultar_humano(self, work, root_page) -> None:
        """Click de mouse real (mejor que JS para reCAPTCHA invisible del SRI)."""
        root_page = root_page or work
        selectors = (
            'button:has-text("Consultar")',
            'input[value="Consultar"]',
            'input[value="CONSULTAR"]',
            '.ui-button:has-text("Consultar")',
            'a:has-text("Consultar")',
            '[id*="btnConsultar" i]',
        )
        for sel in selectors:
            try:
                btn = work.locator(sel).first
                if not btn.count() or not btn.is_visible(timeout=1500):
                    continue
                box = btn.bounding_box()
                if not box:
                    btn.click(delay=random.randint(80, 180))
                    return
                x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
                y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
                try:
                    root_page.mouse.move(x, y, steps=random.randint(8, 18))
                except Exception:
                    root_page.mouse.move(x, y)
                root_page.wait_for_timeout(random.randint(120, 350))
                root_page.mouse.click(x, y, delay=random.randint(50, 150))
                return
            except Exception:
                continue
        self._click_consultar(work)

    def _seleccionar_periodo_mes(self, work, anio: int, mes: int) -> None:
        """Portal SRI: mes completo (dia=Todos, value=0)."""
        mes_nombre = MESES_ES[mes] if 1 <= mes <= 12 else str(mes)
        anio_s = str(anio)
        work.evaluate(
            """({anio, mesNombre, mesNum}) => {
                const selects = Array.from(document.querySelectorAll('select'));
                const setSelect = (sel, wanted) => {
                    if (!sel || wanted === undefined || wanted === null) return false;
                    const w = String(wanted).trim().toLowerCase();
                    for (const opt of sel.options) {
                        const t = (opt.text || '').trim().toLowerCase();
                        const v = (opt.value || '').trim().toLowerCase();
                        if (t === w || v === w || t.includes(w) || v === String(wanted)) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                };
                const byHint = (hint) => selects.filter(s => {
                    const id = (s.id||'').toLowerCase();
                    const name = (s.name||'').toLowerCase();
                    const lbl = (s.labels && s.labels[0] ? s.labels[0].innerText : '').toLowerCase();
                    return id.includes(hint) || name.includes(hint) || lbl.includes(hint);
                });
                const anioSels = byHint('anio').length ? byHint('anio') : byHint('ano');
                const mesSels = byHint('mes');
                const diaSels = byHint('dia');
                for (const s of anioSels) setSelect(s, anio);
                for (const s of mesSels) {
                    if (!setSelect(s, mesNombre)) setSelect(s, mesNum);
                }
                for (const s of diaSels) {
                    if (!setSelect(s, '0')) setSelect(s, 'todos');
                }
            }""",
            {
                "anio": anio_s,
                "mesNombre": mes_nombre,
                "mesNum": str(mes),
            },
        )
        work.wait_for_timeout(800)

    def _consultar_por_mes_auto(
        self,
        work,
        anio: int,
        mes: int,
        tipo_comprobante: str,
        root_page,
    ) -> None:
        intentos = self.config.consulta_retries
        etiqueta = f"{anio}-{mes:02d}"
        for attempt in range(1, intentos + 1):
            self._seleccionar_periodo_mes(work, anio, mes)
            self._seleccionar_tipo_comprobante(work, tipo_comprobante)
            work.wait_for_timeout(random.randint(400, 900))

            try:
                self._aplicar_captcha_antes_consultar(work, root_page)
            except Exception as e:
                print(f"  ERROR captcha: {e}")
                if self.config.consulta_modo == "solver":
                    print("  Reintentando consulta sin token 2captcha...")

            self._click_consultar_humano(work, root_page)
            estado = self._esperar_post_consulta(root_page, work, timeout_sec=75)
            if estado == "ok":
                return
            print(
                f"  Consulta mes {etiqueta} no OK ({estado}), "
                f"intento {attempt}/{intentos}"
            )
            if attempt < intentos:
                work.wait_for_timeout(1500 * attempt)

        raise RuntimeError(
            f"No se pudo consultar mes {etiqueta} tras {intentos} intentos.\n"
            "Renueve sesion (--init-portal-session) o configure SRI_CAPTCHA_2CAPTCHA_KEY."
        )

    def _consultar_por_mes(
        self,
        work,
        anio: int,
        mes: int,
        tipo_comprobante: str = "Factura",
        root_page=None,
    ) -> None:
        root_page = root_page or work
        modo = self.config.consulta_modo
        ref = date(anio, mes, 1)

        if modo == "manual":
            self._seleccionar_periodo_mes(work, anio, mes)
            self._seleccionar_tipo_comprobante(work, tipo_comprobante)
            estado = self._esperar_consulta_manual(
                root_page,
                work,
                ref,
                timeout_sec=self.config.captcha_timeout_sec,
            )
            if estado == "timeout":
                raise RuntimeError(
                    f"Timeout esperando consulta manual del mes {anio}-{mes:02d}.\n"
                    "Haga click en CONSULTAR en Chrome dentro del tiempo indicado."
                )
        else:
            self._consultar_por_mes_auto(work, anio, mes, tipo_comprobante, root_page)

        work.wait_for_timeout(1500)
        try:
            work.wait_for_load_state("networkidle", timeout=45_000)
        except Exception:
            pass

    def _consultar_por_dia_auto(
        self,
        work,
        fecha: date,
        tipo_comprobante: str,
        root_page,
    ) -> None:
        intentos = self.config.consulta_retries
        for attempt in range(1, intentos + 1):
            self._seleccionar_periodo_dia(work, fecha)
            self._seleccionar_tipo_comprobante(work, tipo_comprobante)
            work.wait_for_timeout(random.randint(400, 900))

            try:
                self._aplicar_captcha_antes_consultar(work, root_page)
            except Exception as e:
                print(f"  ERROR captcha: {e}")
                if self.config.consulta_modo == "solver":
                    print("  Reintentando consulta sin token 2captcha...")

            self._click_consultar_humano(work, root_page)
            estado = self._esperar_post_consulta_dia(
                root_page, work, fecha, timeout_sec=55
            )
            if estado == "ok":
                return
            if estado == "captcha" and not self.config.portal_headless:
                print(
                    f"  Captcha SRI — resuelvalo en el navegador y clic en CONSULTAR "
                    f"({fecha.isoformat()})"
                )
                try:
                    self._esperar_captcha_resuelto(
                        root_page, work, self.config.captcha_timeout_sec
                    )
                    estado = self._esperar_post_consulta_dia(
                        root_page, work, fecha, timeout_sec=55
                    )
                    if estado == "ok":
                        return
                except RuntimeError as e:
                    print(f"  WARN captcha manual: {e}")
            elif estado == "stale":
                print(
                    f"  Tabla obsoleta (datos de otro dia), "
                    f"reintento {attempt}/{intentos}"
                )
            else:
                print(
                    f"  Consulta dia {fecha.isoformat()} no OK ({estado}), "
                    f"intento {attempt}/{intentos}"
                )
            if attempt < intentos:
                work.wait_for_timeout(1500 * attempt)

        raise RuntimeError(
            f"No se pudo consultar {fecha.isoformat()} tras {intentos} intentos.\n"
            "Renueve sesion (--init-portal-session) o configure SRI_CAPTCHA_2CAPTCHA_KEY."
        )

    def _consultar_por_dia(
        self,
        work,
        fecha: date,
        tipo_comprobante: str = "Factura",
        root_page=None,
    ) -> None:
        """Portal SRI: un solo dia por consulta (anio + mes + dia)."""
        root_page = root_page or work
        modo = self.config.consulta_modo

        if modo == "manual":
            self._preparar_formulario_siguiente_dia(work, root_page)
            self._seleccionar_periodo_dia(work, fecha)
            self._seleccionar_tipo_comprobante(work, tipo_comprobante)
            estado = self._esperar_consulta_manual(
                root_page,
                work,
                fecha,
                timeout_sec=max(180, self.config.captcha_timeout_sec),
            )
            if estado == "timeout":
                raise RuntimeError(
                    f"Timeout esperando consulta manual del dia {fecha.isoformat()}.\n"
                    "Haga click en CONSULTAR en Chrome dentro del tiempo indicado."
                )
        else:
            self._consultar_por_dia_auto(work, fecha, tipo_comprobante, root_page)

        work.wait_for_timeout(1500)
        try:
            work.wait_for_load_state("networkidle", timeout=45_000)
        except Exception:
            pass

    def _seleccionar_periodo_dia(self, work, fecha: date) -> None:
        mes_nombre = MESES_ES[fecha.month] if 1 <= fecha.month <= 12 else str(fecha.month)
        anio_s = str(fecha.year)
        dia_s = str(fecha.day)
        dia_pad = f"{fecha.day:02d}"

        # Portal SRI actual: frmPrincipal:ano / mes / dia
        try:
            ano = work.locator('[id="frmPrincipal:ano"]')
            mes = work.locator('[id="frmPrincipal:mes"]')
            dia = work.locator('[id="frmPrincipal:dia"]')
            if ano.count() and mes.count() and dia.count():
                ano.select_option(value=anio_s)
                work.wait_for_timeout(300)
                mes.select_option(value=str(fecha.month))
                work.wait_for_timeout(300)
                dia.select_option(value=dia_s)
                work.wait_for_timeout(500)
                print(
                    f"    Fecha ajustada: {fecha.isoformat()} "
                    f"({mes_nombre} {fecha.day}, {fecha.year})"
                )
                return
        except Exception:
            pass

        work.evaluate(
            """({anio, mesNombre, mesNum, dia, diaPad}) => {
                const selects = Array.from(document.querySelectorAll('select'));
                const setSelect = (sel, wanted) => {
                    if (!sel || wanted === undefined || wanted === null) return false;
                    const w = String(wanted).trim().toLowerCase();
                    for (const opt of sel.options) {
                        const t = (opt.text || '').trim().toLowerCase();
                        const v = (opt.value || '').trim().toLowerCase();
                        if (t === w || v === w || t.includes(w) || v === String(wanted)) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                };
                const byHint = (hint) => selects.filter(s => {
                    const id = (s.id||'').toLowerCase();
                    const name = (s.name||'').toLowerCase();
                    const lbl = (s.labels && s.labels[0] ? s.labels[0].innerText : '').toLowerCase();
                    return id.includes(hint) || name.includes(hint) || lbl.includes(hint);
                });
                const anioSels = byHint('anio').length ? byHint('anio') : byHint('ano');
                const mesSels = byHint('mes');
                const diaSels = byHint('dia');
                for (const s of anioSels) setSelect(s, anio);
                for (const s of mesSels) {
                    if (!setSelect(s, mesNombre)) setSelect(s, mesNum);
                }
                for (const s of diaSels) {
                    if (!setSelect(s, dia)) setSelect(s, diaPad);
                }
            }""",
            {
                "anio": anio_s,
                "mesNombre": mes_nombre,
                "mesNum": str(fecha.month),
                "dia": dia_s,
                "diaPad": dia_pad,
            },
        )
        work.wait_for_timeout(800)

    def _preparar_formulario_siguiente_dia(self, work, root_page) -> None:
        """Tras una consulta, vuelve al formulario para el siguiente día."""
        try:
            work.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        for sel in (
            'button:has-text("Nueva consulta")',
            'a:has-text("Nueva consulta")',
            'button:has-text("Limpiar")',
            '.ui-button:has-text("Limpiar")',
            'input[value="Limpiar"]',
        ):
            try:
                loc = work.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1200):
                    loc.click()
                    work.wait_for_timeout(900)
                    break
            except Exception:
                continue
        try:
            work.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll(
                        'button, input[type="submit"], input[type="button"], a'
                    ));
                    for (const el of nodes) {
                        const t = (el.innerText || el.value || '').trim();
                        if (/^consultar$/i.test(t)) {
                            el.scrollIntoView({block: 'center', behavior: 'instant'});
                            return;
                        }
                    }
                }"""
            )
        except Exception:
            pass
        work.wait_for_timeout(500)

    def _click_consultar(self, work) -> None:
        clicked = work.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll(
                    'button, input[type="submit"], input[type="button"], a, span.ui-button-text'
                ));
                for (const el of nodes) {
                    const t = (el.innerText || el.value || '').trim();
                    if (/^consultar$/i.test(t)) {
                        (el.closest('button') || el.closest('a') || el).click();
                        return true;
                    }
                }
                return false;
            }"""
        )
        if clicked:
            return
        for sel in (
            '[id="frmPrincipal:btnBuscar"]',
            'button:has-text("Consultar")',
            '.ui-button:has-text("Consultar")',
            'input[value="Consultar"]',
            'input[value="CONSULTAR"]',
            'a:has-text("Consultar")',
            '[id*="btnConsultar" i]',
            '[id*="consultar" i]',
        ):
            try:
                btn = work.locator(sel).first
                if btn.count() and btn.is_visible(timeout=2000):
                    btn.click()
                    return
            except Exception:
                continue
        raise RuntimeError("No se encontro boton Consultar en portal SRI")

    def _seleccionar_tipo_comprobante(self, work, tipo: str) -> None:
        try:
            combo = work.locator('[id="frmPrincipal:cmbTipoComprobante"]')
            if combo.count():
                tipo_map = {
                    "factura": "1",
                    "liquidación de compra de bienes y prestación de servicios": "2",
                    "liquidacion de compra": "2",
                    "notas de crédito": "3",
                    "notas de credito": "3",
                    "notas de débito": "4",
                    "notas de debito": "4",
                }
                val = tipo_map.get((tipo or "Factura").strip().lower())
                if val:
                    combo.select_option(value=val)
                else:
                    combo.select_option(label=tipo)
                work.wait_for_timeout(400)
                return
        except Exception:
            pass
        opciones = [tipo, "Factura", "FACTURA", "Todos", "TODOS"]
        work.evaluate(
            """(opciones) => {
                const selects = Array.from(document.querySelectorAll('select'));
                const tipoSels = selects.filter(s => {
                    const id = (s.id||'').toLowerCase();
                    const name = (s.name||'').toLowerCase();
                    return id.includes('tipo') || name.includes('tipo') || id.includes('comprob');
                });
                const pick = (sel, wanted) => {
                    const w = String(wanted).trim().toLowerCase();
                    for (const opt of sel.options) {
                        const t = (opt.text||'').trim().toLowerCase();
                        if (t === w || t.includes(w)) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                };
                for (const sel of tipoSels) {
                    for (const op of opciones) {
                        if (pick(sel, op)) return;
                    }
                }
            }""",
            opciones,
        )
        work.wait_for_timeout(500)

    def _parsear_filas_tabla(self, work) -> list[dict[str, Any]]:
        try:
            filas = work.evaluate(_JS_FILAS_TABLA)
            return [f for f in (filas or []) if isinstance(f, dict)]
        except Exception:
            return []

    def _obtener_resultados_dia(
        self,
        work,
        root_page,
        fecha: date,
        *,
        tipo_comprobante: str = "Factura",
        descargar_xml: bool = False,
    ) -> list[ComprobanteRecibido]:
        """Lee tabla tras consulta diaria; reintenta si el portal devuelve filas de otro día."""
        for intento in range(1, 3):
            found = self._obtener_resultados(
                work, root_page, descargar_xml=descargar_xml
            )
            ok = [c for c in found if _fecha_comprobante(c) == fecha]
            if not found or ok:
                if found and len(ok) < len(found):
                    print(
                        f"    Descartadas {len(found) - len(ok)} fila(s) "
                        f"fuera de {fecha.isoformat()}"
                    )
                return ok
            print(
                f"    WARN tabla sin datos de {fecha.isoformat()}; "
                f"re-consulta ({intento}/2)"
            )
            self._consultar_por_dia_auto(
                work, fecha, tipo_comprobante, root_page
            )
        return []

    def _pagina_siguiente_disponible(self, work) -> bool:
        for sel in (
            ".ui-paginator-next:not(.ui-state-disabled)",
            "a.ui-paginator-next:not(.ui-state-disabled)",
            'button[aria-label="Next Page"]:not([disabled])',
        ):
            try:
                loc = work.locator(sel).first
                if loc.count() and loc.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        return False

    def _ir_pagina_siguiente(self, work, root_page) -> bool:
        if not self._pagina_siguiente_disponible(work):
            return False
        baseline = self._parsear_filas_tabla(work)
        baseline_txt = "|".join(
            str(r.get("clave") or r.get("idx")) for r in baseline[:3]
        )
        for sel in (
            ".ui-paginator-next:not(.ui-state-disabled)",
            "a.ui-paginator-next:not(.ui-state-disabled)",
            'button[aria-label="Next Page"]:not([disabled])',
        ):
            try:
                loc = work.locator(sel).first
                if not loc.count() or not loc.is_visible(timeout=800):
                    continue
                loc.click()
                work.wait_for_timeout(1200)
                try:
                    work.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                nuevo = self._parsear_filas_tabla(work)
                nuevo_txt = "|".join(
                    str(r.get("clave") or r.get("idx")) for r in nuevo[:3]
                )
                if nuevo and nuevo_txt != baseline_txt:
                    return True
            except Exception:
                continue
        return False

    def _filas_a_comprobantes(
        self,
        work,
        root_page,
        filas: list[dict[str, Any]],
        *,
        descargar_xml: bool,
    ) -> list[ComprobanteRecibido]:
        out: list[ComprobanteRecibido] = []
        for fila in filas:
            clave = str(fila.get("clave") or "").strip()
            if not clave:
                continue
            comp = ComprobanteRecibido(
                clave_acceso=clave,
                num_factura=str(fila.get("num_factura") or "").strip(),
                ruc_emisor=str(fila.get("ruc_emisor") or "").strip(),
                razon_social=str(fila.get("razon_social") or "").strip(),
            )
            if descargar_xml and fila.get("has_xml_link"):
                link_id = str(fila.get("xml_link_id") or "").strip()
                try:
                    print(f"      XML portal fila {fila.get('idx')}: {clave[:12]}...")
                    xml = self._descargar_xml_fila_portal(
                        work,
                        root_page,
                        row_index=int(fila.get("idx") or 0),
                        link_id=link_id,
                        clave=clave,
                    )
                    comp.extra["xml_autorizado"] = xml
                    comp.extra["descarga_origen"] = "portal"
                except Exception as e:
                    comp.extra["xml_error"] = str(e)
                    print(f"      WARN XML portal: {e}")
            out.append(comp)
        return out

    def _obtener_resultados_tabla_paginada(
        self,
        work,
        root_page,
        *,
        descargar_xml: bool = False,
    ) -> list[ComprobanteRecibido]:
        """Recorre todas las paginas del datatable SRI (evita perder dias fin de mes)."""
        out: list[ComprobanteRecibido] = []
        vistos: set[str] = set()
        pagina = 1
        while pagina <= 40:
            filas = self._parsear_filas_tabla(work)
            if not filas:
                break
            nuevos = 0
            for comp in self._filas_a_comprobantes(
                work, root_page, filas, descargar_xml=descargar_xml
            ):
                if comp.clave_acceso in vistos:
                    continue
                vistos.add(comp.clave_acceso)
                out.append(comp)
                nuevos += 1
            print(f"    Pagina {pagina}: {len(filas)} fila(s), {nuevos} nueva(s)")
            if not self._ir_pagina_siguiente(work, root_page):
                break
            pagina += 1
        return out

    def _cerrar_dialogo_xml(self, work) -> None:
        for sel in (
            '.ui-dialog:has-text("Archivo XML") .ui-dialog-titlebar-close',
            ".ui-dialog .ui-dialog-titlebar-close",
        ):
            try:
                btn = work.locator(sel).first
                if btn.count() and btn.is_visible(timeout=800):
                    btn.click()
                    work.wait_for_timeout(400)
                    return
            except Exception:
                continue

    def _descargar_xml_fila_portal(
        self,
        work,
        root_page,
        *,
        row_index: int | None = None,
        link_id: str = "",
        clave: str = "",
    ) -> str:
        if link_id:
            link = work.locator(f'[id="{link_id}"]')
        else:
            row = work.locator(".ui-datatable-data tr, table.ui-datatable-data tbody tr").nth(
                row_index or 0
            )
            link = row.locator(
                'a[id*="lnkXml"], a[id*="lnkDocumento"], a[onclick*="lnkXml"]'
            ).first
        if not link.count():
            raise RuntimeError("No se encontro enlace XML en la fila del portal")
        self._cerrar_dialogo_xml(work)
        link.scroll_into_view_if_needed()
        try:
            link.click(force=True, timeout=10_000)
        except Exception:
            link.click(timeout=10_000)
        dialog = work.locator(".ui-dialog").filter(has_text="Archivo XML").first
        try:
            dialog.wait_for(state="visible", timeout=12_000)
        except Exception:
            work.evaluate(
                """() => {
                    for (const d of document.querySelectorAll('.ui-dialog')) {
                        d.classList.remove('ui-overlay-hidden');
                        d.style.display = 'block';
                        d.style.visibility = 'visible';
                        d.style.zIndex = '9999';
                    }
                }"""
            )
            work.wait_for_timeout(600)
            dialog.wait_for(state="visible", timeout=15_000)
        btn = dialog.locator(
            'input[value="Descargar"], button:has-text("Descargar")'
        ).first
        if not btn.count():
            raise RuntimeError("Dialogo Archivo XML sin boton Descargar")
        temp = Path(os.getenv("TEMP", ".")) / f"sri_xml_{int(time.time() * 1000)}.xml"
        content = ""
        try:
            try:
                with root_page.expect_download(timeout=45_000) as dl_info:
                    btn.click()
                download = dl_info.value
                download.save_as(str(temp))
                content = temp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                with work.expect_response(
                    lambda r: "xml" in (r.headers.get("content-type") or "").lower()
                    or bool(re.search(r"\.xml(\?|$)", r.url, re.I)),
                    timeout=45_000,
                ) as resp_info:
                    btn.click()
                resp = resp_info.value
                content = resp.text()
        finally:
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            self._cerrar_dialogo_xml(work)
        clave_norm = _normalizar_clave(clave) if clave else ""
        if not clave_norm:
            m = CLAVE_ACCESO_RE.search(content)
            clave_norm = m.group(1) if m else ""
        if not clave_norm:
            raise RuntimeError("No se pudo determinar clave de acceso del XML portal")
        return _normalizar_xml_descargado(clave_norm, content)

    def _obtener_resultados(
        self,
        work,
        root_page,
        *,
        descargar_xml: bool = False,
    ) -> list[ComprobanteRecibido]:
        out = self._obtener_resultados_tabla_paginada(
            work, root_page, descargar_xml=descargar_xml
        )
        if out:
            return out

        # 1) Descargar reporte TXT (click en frame de trabajo, download en pagina raiz)
        for sel in (
            'a:has-text("Descargar reporte")',
            'button:has-text("Descargar reporte")',
            'a:has-text("DESCARGAR REPORTE")',
            'button:has-text("DESCARGAR REPORTE")',
            'span:has-text("Descargar reporte")',
        ):
            try:
                loc = work.locator(sel).first
                if not loc.count() or not loc.is_visible(timeout=2000):
                    continue
                with root_page.expect_download(timeout=30_000) as dl_info:
                    loc.click()
                download = dl_info.value
                suffix = Path(download.suggested_filename or "reporte.txt").suffix or ".txt"
                download_path = (
                    Path(os.getenv("TEMP", ".")) / f"sri_reporte_{int(time.time())}{suffix}"
                )
                download.save_as(str(download_path))
                contenido = download_path.read_text(encoding="utf-8", errors="replace")
                try:
                    download_path.unlink(missing_ok=True)
                except Exception:
                    pass
                parsed = _parsear_reporte_txt(contenido)
                if parsed:
                    return parsed
            except Exception:
                continue

        # 2) Tabla HTML / texto visible
        html = work.content() if hasattr(work, "content") else root_page.content()
        parsed = _parsear_html_tabla(html)
        if parsed:
            return parsed

        try:
            texto = work.inner_text("body")
        except Exception:
            texto = root_page.inner_text("body")
        claves = CLAVE_ACCESO_RE.findall(texto)
        return [ComprobanteRecibido(clave_acceso=c) for c in dict.fromkeys(claves)]


def metadata_desde_xml(xml_texto: str) -> dict[str, str]:
    """Extrae campos básicos del XML autorizado sin procesar ítems."""
    try:
        root = ET.fromstring(xml_texto)
    except ET.ParseError:
        return {}

    comp = root.find(".//comprobante")
    inner = comp.text.strip() if comp is not None and comp.text else xml_texto
    try:
        fact = ET.fromstring(inner)
    except ET.ParseError:
        return {}

    it = fact.find(".//infoTributaria")
    if it is None:
        return {}

    def txt(el, tag):
        n = el.find(tag)
        return (n.text or "").strip() if n is not None else ""

    estab = txt(it, "estab")
    pto = txt(it, "ptoEmi")
    sec = txt(it, "secuencial")
    inf = fact.find(".//infoFactura")
    fecha = txt(inf, "fechaEmision") if inf is not None else ""

    return {
        "num_factura": f"{estab}-{pto}-{sec}" if estab else "",
        "ruc_emisor": txt(it, "ruc"),
        "razon_social": txt(it, "razonSocial"),
        "fecha_emision": fecha,
    }

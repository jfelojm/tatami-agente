"""
Cliente SRI Ecuador: listado portal (Playwright) + descarga XML (SOAP zeep).

Portal: comprobantes recibidos → claves de acceso (49 dígitos).
SOAP: AutorizacionComprobantesOffline.autorizacionComprobante(clave).
"""
from __future__ import annotations

import os
import re
import time
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
            captcha_to = int(os.getenv("SRI_CAPTCHA_TIMEOUT_SEC") or "180")
        except ValueError:
            captcha_to = 180
        browser = (os.getenv("SRI_PORTAL_BROWSER") or "chrome").strip().lower()
        if browser not in ("chrome", "chromium", "msedge"):
            browser = "chrome"
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
        kwargs: dict[str, Any] = {
            "locale": "es-EC",
            "timezone_id": "America/Guayaquil",
            "accept_downloads": True,
            "user_agent": SRI_PORTAL_USER_AGENT,
            "viewport": {"width": 1366, "height": 900},
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignore_default_args": ["--enable-automation"],
        }
        browser_name = self.config.portal_browser
        if browser_name in ("chrome", "msedge"):
            kwargs["channel"] = browser_name
        try:
            ctx = p.chromium.launch_persistent_context(str(profile), **kwargs)
            print(f"Navegador: {browser_name} (perfil {profile.name})")
        except Exception:
            if browser_name != "chromium":
                kwargs.pop("channel", None)
                ctx = p.chromium.launch_persistent_context(str(profile), **kwargs)
                print(f"Navegador: chromium (perfil {profile.name})")
            else:
                raise
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
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
            launch_kw: dict[str, Any] = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled"],
                "ignore_default_args": ["--enable-automation"],
            }
            if browser_name in ("chrome", "msedge"):
                launch_kw["channel"] = browser_name
            try:
                browser = p.chromium.launch(**launch_kw)
            except Exception:
                launch_kw.pop("channel", None)
                browser = p.chromium.launch(**launch_kw)
            ctx = browser.new_context(**kwargs, storage_state=str(state))
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
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignore_default_args": ["--enable-automation"],
        }
        if browser_name in ("chrome", "msedge"):
            launch_kwargs["channel"] = browser_name

        try:
            browser = p.chromium.launch(**launch_kwargs)
            print(f"Navegador: {browser_name}")
            return browser
        except Exception as e:
            if browser_name != "chromium":
                print(
                    f"WARN: no se pudo abrir {browser_name} ({e}). "
                    "Probando chromium embebido..."
                )
                launch_kwargs.pop("channel", None)
                return p.chromium.launch(**launch_kwargs)
            raise

    def _pagina_trabajo(self, page):
        """Devuelve frame con el formulario de consulta (iframe o pagina principal)."""
        for frame in page.frames:
            url = (frame.url or "").lower()
            if "comprobantes-electronicos" in url or "consulta" in url:
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
        page.wait_for_timeout(3000)

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
            work = self._pagina_trabajo(page)
            if self._en_formulario_consulta(work):
                return work
            page.wait_for_timeout(2000)

        raise RuntimeError(
            "Timeout esperando formulario de comprobantes recibidos en portal SRI."
        )

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

    def _en_formulario_consulta(self, work) -> bool:
        try:
            return work.locator("select").count() >= 2
        except Exception:
            return False

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
            page.goto(SRI_MENU_URL, wait_until="commit", timeout=120_000)
            page.wait_for_timeout(3000)
            print("")
            print("=" * 60)
            print("INSTRUCCIONES (no cierre Chrome todavia):")
            print("  1. Si pide login: RUC + clave (+ captcha si aparece)")
            print("  2. En el menu izquierdo abra Facturacion electronica")
            print("  3. Click en 'Comprobantes electronicos recibidos'")
            print("  4. Si pide login otra vez, repita credenciales")
            print("  5. Confirme que ve selects de ANIO / MES / TIPO")
            print("  6. Vuelva a PowerShell y presione ENTER")
            print("=" * 60)
            print("")
            try:
                input("Presione ENTER cuando vea el formulario de comprobantes recibidos... ")
            except EOFError:
                print("WARN: stdin no disponible; esperando 180s para login manual...")
                page.wait_for_timeout(180_000)

            try:
                work = self._pagina_trabajo(page)
                if not self._en_formulario_consulta(work):
                    work = self._navegar_a_formulario_recibidos(page)
            except Exception:
                work = self._pagina_trabajo(page)
            if self._necesita_login(page):
                self._close_context(ctx)
                raise RuntimeError(
                    "Sesion NO guardada: sigue en pantalla de login.\n"
                    "Ejecute de nuevo e inicie sesion antes de presionar ENTER."
                )
            if not self._en_formulario_consulta(work):
                self._guardar_debug(page, "init_sin_formulario")
                self._close_context(ctx)
                raise RuntimeError(
                    "Sesion NO guardada: no se detecto formulario de comprobantes recibidos.\n"
                    "Navegue hasta esa pantalla y ejecute --init-portal-session otra vez."
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
    ) -> list[ComprobanteRecibido]:
        if not self.config.sesion_portal_guardada():
            raise RuntimeError(
                "No hay sesion del portal SRI.\n"
                "Ejecute primero: .\\ejecutar_facturas_sri.ps1 --init-portal-session"
            )

        with self._playwright()() as p:
            ctx, page = self._open_context(p, headless=self.config.portal_headless)

            work = self._navegar_a_formulario_recibidos(page)
            print("  Formulario de consulta listo.")

            comprobantes: list[ComprobanteRecibido] = []
            meses = _meses_en_rango(fecha_desde, fecha_hasta)
            for anio, mes in meses:
                print(f"  Consultando periodo: {MESES_ES[mes]} {anio}")
                self._consultar_por_fechas(
                    work,
                    anio,
                    mes,
                    tipo_comprobante=tipo_comprobante,
                )
                found = self._obtener_resultados(work, page)
                print(f"    -> {len(found)} comprobante(s)")
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

        if self._pagina_pide_captcha(page):
            if self.config.portal_headless:
                raise RuntimeError(
                    "SRI pide captcha en login. Ejecute:\n"
                    "  .\\ejecutar_facturas_sri.ps1 --init-portal-session"
                )
            print(f"Resuelva captcha ({self.config.captcha_timeout_sec}s)...")
            page.wait_for_timeout(self.config.captcha_timeout_sec * 1000)
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

    def _pagina_pide_captcha(self, page) -> bool:
        try:
            return page.locator('iframe[src*="recaptcha"]').count() > 0
        except Exception:
            return False

    def _consultar_por_fechas(
        self,
        work,
        anio: int,
        mes: int,
        tipo_comprobante: str = "Factura",
    ) -> None:
        self._seleccionar_periodo_mes(work, anio, mes)
        self._seleccionar_tipo_comprobante(work, tipo_comprobante)
        self._click_consultar(work)

        if self._pagina_pide_captcha(work):
            if self.config.portal_headless:
                raise RuntimeError(
                    "Captcha en consulta. Ejecute --init-portal-session de nuevo."
                )
            print("Resuelva captcha de consulta...")
            work.wait_for_timeout(self.config.captcha_timeout_sec * 1000)

        work.wait_for_timeout(3000)
        try:
            work.wait_for_load_state("networkidle", timeout=45_000)
        except Exception:
            pass
        try:
            work.wait_for_selector(
                "table tbody tr, .ui-datatable-data tr, .ui-widget-content tr",
                timeout=20_000,
            )
        except Exception:
            pass

    def _seleccionar_periodo_mes(self, work, anio: int, mes: int) -> None:
        mes_nombre = MESES_ES[mes] if 1 <= mes <= 12 else str(mes)
        anio_s = str(anio)

        work.evaluate(
            """({anio, mesNombre, mesNum}) => {
                const selects = Array.from(document.querySelectorAll('select'));
                const setSelect = (sel, wanted) => {
                    if (!sel || !wanted) return false;
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
                    if (!setSelect(s, 'Todos')) setSelect(s, 'TODOS');
                }
            }""",
            {"anio": anio_s, "mesNombre": mes_nombre, "mesNum": str(mes)},
        )
        work.wait_for_timeout(800)

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
            '.ui-button:has-text("Consultar")',
            'button:has-text("Consultar")',
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

    def _obtener_resultados(
        self, work, root_page
    ) -> list[ComprobanteRecibido]:
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

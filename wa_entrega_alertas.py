"""
Entrega de alertas WA según configuración (.env), respetando política Meta.

Dentro de ventana 24h: texto libre.
Fuera de ventana:
  1. Si WHATSAPP_TEMPLATE_ALERTA está definida → plantilla utilitaria con {{1}} = cuerpo.
  2. Si no → encola en Supabase y envía tatami_bienvenida (una vez / 24h).
     Al responder al bot, el webhook entrega la cola automáticamente.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    requests = None

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
except ImportError:
    pass

LOG_WEBHOOK = Path(__file__).resolve().parent / "logs" / "webhook_inbound.log"
LOG_PENDIENTES = Path(__file__).resolve().parent / "logs" / "wa_alertas_pendientes.json"
STORAGE_BUCKET = (os.getenv("TATAMI_WA_STORAGE_BUCKET") or "tatami-wa").strip()
STORAGE_CONTACTO = "wa_contacto.json"
STORAGE_PENDIENTES = "wa_alertas_pendientes.json"
TZ_GYE = ZoneInfo("America/Guayaquil")
WA_GRAPH_VERSION = os.getenv("WHATSAPP_API_VERSION", "v25.0").strip() or "v25.0"

_sb = None
_tablas_ok: bool | None = None
_storage_ok: bool | None = None


def _solo_digitos(numero: str) -> str:
    return "".join(c for c in (numero or "") if c.isdigit())


def _sb_client():
    global _sb
    if _sb is not None:
        return _sb
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client

        _sb = create_client(url, key)
        return _sb
    except Exception:
        return None


def _supabase_storage_ok() -> bool:
    global _storage_ok
    if _storage_ok is not None:
        return _storage_ok
    sb = _sb_client()
    if not sb:
        _storage_ok = False
        return False
    try:
        sb.storage.from_(STORAGE_BUCKET).list("", {"limit": 1})
        _storage_ok = True
    except Exception:
        _storage_ok = False
    return _storage_ok


def _storage_read_json(path: str) -> dict:
    if not _supabase_storage_ok():
        return {}
    try:
        raw = _sb_client().storage.from_(STORAGE_BUCKET).download(path)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _storage_write_json(path: str, data: dict) -> bool:
    if not _supabase_storage_ok():
        return False
    try:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        _sb_client().storage.from_(STORAGE_BUCKET).upload(
            path,
            payload,
            file_options={"content-type": "application/json", "upsert": "true"},
        )
        return True
    except Exception as e:
        print(f"  WARN storage write {path}: {e}")
        return False


def _contacto_storage() -> dict:
    return _storage_read_json(STORAGE_CONTACTO)


def _guardar_contacto_storage(data: dict) -> None:
    _storage_write_json(STORAGE_CONTACTO, data)


def _pendientes_storage() -> dict:
    return _storage_read_json(STORAGE_PENDIENTES)


def _guardar_pendientes_storage(data: dict) -> None:
    _storage_write_json(STORAGE_PENDIENTES, data)


def _supabase_tablas_ok() -> bool:
    global _tablas_ok
    if _tablas_ok is not None:
        return _tablas_ok
    sb = _sb_client()
    if not sb:
        _tablas_ok = False
        return False
    try:
        sb.table("tatami_wa_contacto").select("wa_id").limit(1).execute()
        sb.table("tatami_wa_alertas_pendientes").select("id").limit(1).execute()
        _tablas_ok = True
    except Exception:
        _tablas_ok = False
    return _tablas_ok


def _utcnow() -> datetime:
    return datetime.now(TZ_GYE)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ_GYE).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def _ultimo_inbound_log_local(numero: str) -> datetime | None:
    digits = _solo_digitos(numero)
    if not digits or not LOG_WEBHOOK.is_file():
        return None
    pat = re.compile(
        rf"^(\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}}) IN from={re.escape(digits)} "
    )
    ultimo: datetime | None = None
    try:
        lines = LOG_WEBHOOK.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in lines[-1200:]:
        m = pat.match(line)
        if m:
            ultimo = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return ultimo


def _ultimo_inbound_supabase(numero: str) -> datetime | None:
    wa_id = _solo_digitos(numero)
    if _supabase_tablas_ok():
        try:
            r = (
                _sb_client()
                .table("tatami_wa_contacto")
                .select("last_inbound_at")
                .eq("wa_id", wa_id)
                .limit(1)
                .execute()
            )
            rows = r.data or []
            if rows:
                return _parse_ts(rows[0].get("last_inbound_at"))
        except Exception:
            pass
    if _supabase_storage_ok():
        row = (_contacto_storage().get(wa_id) or {})
        return _parse_ts(row.get("last_inbound_at"))
    return None


def registrar_inbound(wa_id: str) -> None:
    """Llamar en cada mensaje entrante al bot (webhook Meta)."""
    digits = _solo_digitos(wa_id)
    if not digits:
        return
    now_iso = _utcnow().isoformat()
    if _supabase_tablas_ok():
        try:
            _sb_client().table("tatami_wa_contacto").upsert(
                {"wa_id": digits, "last_inbound_at": now_iso, "updated_at": now_iso},
                on_conflict="wa_id",
            ).execute()
        except Exception as e:
            print(f"  WARN wa_contacto upsert: {e}")
    if _supabase_storage_ok():
        data = _contacto_storage()
        row = data.get(digits) or {}
        row["last_inbound_at"] = now_iso
        data[digits] = row
        _guardar_contacto_storage(data)


def usuario_en_ventana_24h(numero: str, *, horas: float = 24.0) -> bool:
    ultimo = _ultimo_inbound_supabase(numero) or _ultimo_inbound_log_local(numero)
    if not ultimo:
        return False
    ahora = _utcnow().replace(tzinfo=None)
    return (ahora - ultimo) < timedelta(hours=horas)


def _template_alerta_config() -> tuple[str, str] | None:
    name = (os.getenv("WHATSAPP_TEMPLATE_ALERTA") or "").strip()
    if not name:
        return None
    lang = (os.getenv("WHATSAPP_TEMPLATE_ALERTA_LANG") or "es_EC").strip() or "es_EC"
    return name, lang


def _wa_disabled() -> bool:
    return (os.getenv("TATAMI_WA_SKIP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "si",
        "sí",
    )


def enviar_plantilla_bienvenida(numero: str) -> tuple[bool, str]:
    return _enviar_plantilla_simple(numero, "tatami_bienvenida", "es_EC")


def _enviar_plantilla_simple(numero: str, name: str, lang: str) -> tuple[bool, str]:
    if _wa_disabled():
        return False, "TATAMI_WA_SKIP activo"
    if not requests:
        return False, "requests no instalado"
    pid = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    tok = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not pid or not tok:
        return False, "sin credenciales WA"
    url = f"https://graph.facebook.com/{WA_GRAPH_VERSION}/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": _solo_digitos(numero),
        "type": "template",
        "template": {"name": name, "language": {"code": lang}},
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code >= 400:
            return False, r.text[:240]
        return True, f"template {name}"
    except Exception as e:
        return False, str(e)


def enviar_plantilla_alerta(numero: str, cuerpo: str) -> tuple[bool, str]:
    """Plantilla utilitaria con variable {{1}} (WHATSAPP_TEMPLATE_ALERTA)."""
    cfg = _template_alerta_config()
    if not cfg:
        return False, "WHATSAPP_TEMPLATE_ALERTA no configurada"
    name, lang = cfg
    if _wa_disabled():
        return False, "TATAMI_WA_SKIP activo"
    if not requests:
        return False, "requests no instalado"
    pid = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    tok = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not pid or not tok:
        return False, "sin credenciales WA"
    texto = (cuerpo or "").strip()[:1024]
    url = f"https://graph.facebook.com/{WA_GRAPH_VERSION}/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": _solo_digitos(numero),
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": texto}],
                }
            ],
        },
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code >= 400:
            return False, r.text[:240]
        return True, f"template {name}"
    except Exception as e:
        return False, str(e)


def _plantilla_reciente(numero: str, *, horas: float = 24.0) -> bool:
    wa_id = _solo_digitos(numero)
    ts: datetime | None = None
    if _supabase_tablas_ok():
        try:
            r = (
                _sb_client()
                .table("tatami_wa_contacto")
                .select("template_enviada_at")
                .eq("wa_id", wa_id)
                .limit(1)
                .execute()
            )
            rows = r.data or []
            if rows:
                ts = _parse_ts(rows[0].get("template_enviada_at"))
        except Exception:
            pass
    if ts is None and _supabase_storage_ok():
        row = (_contacto_storage().get(wa_id) or {})
        ts = _parse_ts(row.get("template_enviada_at"))
    if ts and (_utcnow().replace(tzinfo=None) - ts) < timedelta(hours=horas):
        return True
    return False


def _marcar_plantilla_enviada(numero: str) -> None:
    wa_id = _solo_digitos(numero)
    now_iso = _utcnow().isoformat()
    if _supabase_tablas_ok():
        try:
            _sb_client().table("tatami_wa_contacto").upsert(
                {
                    "wa_id": wa_id,
                    "template_enviada_at": now_iso,
                    "updated_at": now_iso,
                },
                on_conflict="wa_id",
            ).execute()
        except Exception:
            pass
    if _supabase_storage_ok():
        data = _contacto_storage()
        row = data.get(wa_id) or {}
        row["template_enviada_at"] = now_iso
        data[wa_id] = row
        _guardar_contacto_storage(data)


def _encolar_local(numero: str, cuerpo: str, etiqueta: str, origen: str) -> None:
    wa_id = _solo_digitos(numero)
    data: dict = {}
    if LOG_PENDIENTES.is_file():
        try:
            data = json.loads(LOG_PENDIENTES.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    cola = data.setdefault(wa_id, [])
    cola.append(
        {
            "cuerpo": cuerpo,
            "etiqueta": etiqueta,
            "origen": origen,
            "creado_at": _utcnow().isoformat(),
        }
    )
    LOG_PENDIENTES.parent.mkdir(exist_ok=True)
    LOG_PENDIENTES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def encolar_alerta(
    numero: str,
    cuerpo: str,
    *,
    etiqueta: str = "alerta",
    origen: str = "pipeline",
) -> bool:
    wa_id = _solo_digitos(numero)
    body = (cuerpo or "").strip()
    if not wa_id or not body:
        return False
    item = {
        "cuerpo": body[:4096],
        "etiqueta": etiqueta,
        "origen": origen,
        "creado_at": _utcnow().isoformat(),
    }
    if _supabase_tablas_ok():
        try:
            _sb_client().table("tatami_wa_alertas_pendientes").insert(
                {
                    "wa_id": wa_id,
                    "cuerpo": body[:4096],
                    "etiqueta": etiqueta,
                    "origen": origen,
                }
            ).execute()
            return True
        except Exception as e:
            print(f"  WARN encolar alerta Supabase: {e}")
    if _supabase_storage_ok():
        data = _pendientes_storage()
        cola = data.setdefault(wa_id, [])
        item["id"] = f"st-{len(cola)}"
        cola.append(item)
        if _guardar_pendientes_storage(data):
            return True
    _encolar_local(wa_id, body[:4096], etiqueta, origen)
    return True


def _listar_pendientes_supabase(wa_id: str) -> list[dict]:
    try:
        r = (
            _sb_client()
            .table("tatami_wa_alertas_pendientes")
            .select("id,cuerpo,etiqueta,origen,creado_at")
            .eq("wa_id", wa_id)
            .is_("entregado_at", "null")
            .order("creado_at")
            .limit(50)
            .execute()
        )
        return r.data or []
    except Exception:
        return []


def _listar_pendientes_local(wa_id: str) -> list[dict]:
    if not LOG_PENDIENTES.is_file():
        return []
    try:
        data = json.loads(LOG_PENDIENTES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get(wa_id) or []
    return [{"id": f"local-{i}", **it} for i, it in enumerate(items)]


def _limpiar_pendientes_local(wa_id: str) -> None:
    if not LOG_PENDIENTES.is_file():
        return
    try:
        data = json.loads(LOG_PENDIENTES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if wa_id in data:
        del data[wa_id]
        LOG_PENDIENTES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _limpiar_pendientes_storage(wa_id: str) -> None:
    if not _supabase_storage_ok():
        return
    data = _pendientes_storage()
    if wa_id in data:
        del data[wa_id]
        _guardar_pendientes_storage(data)


def _listar_pendientes_storage(wa_id: str) -> list[dict]:
    return (_pendientes_storage().get(wa_id) or [])


def listar_alertas_pendientes(numero: str) -> list[dict]:
    wa_id = _solo_digitos(numero)
    if _supabase_tablas_ok():
        items = _listar_pendientes_supabase(wa_id)
        if items:
            return items
    if _supabase_storage_ok():
        items = _listar_pendientes_storage(wa_id)
        if items:
            return items
    return _listar_pendientes_local(wa_id)


def marcar_entregadas(ids: list[str]) -> None:
    supabase_ids = [i for i in ids if not str(i).startswith(("local-", "st-"))]
    storage_ids = [i for i in ids if str(i).startswith("st-")]
    if supabase_ids and _supabase_tablas_ok():
        now_iso = _utcnow().isoformat()
        for pid in supabase_ids:
            try:
                _sb_client().table("tatami_wa_alertas_pendientes").update(
                    {"entregado_at": now_iso}
                ).eq("id", pid).execute()
            except Exception:
                pass
    if storage_ids and _supabase_storage_ok():
        data = _pendientes_storage()
        for wa_id, items in list(data.items()):
            rest = [it for it in items if str(it.get("id")) not in storage_ids]
            if rest:
                data[wa_id] = rest
            else:
                data.pop(wa_id, None)
        _guardar_pendientes_storage(data)


def enviar_alerta_wa_configurada(
    numero_raw: str,
    cuerpo: str,
    *,
    etiqueta: str = "alerta",
    origen: str = "pipeline",
) -> tuple[bool, str, bool]:
    """
    Entrega alerta según config y ventana Meta.
    Retorna (ok, detalle, texto_entregado).
    """
    from alertas_tatami import enviar_whatsapp_texto, log_envio_wa

    body = (cuerpo or "").strip()
    if not body:
        return False, "cuerpo vacio", False

    if usuario_en_ventana_24h(numero_raw):
        ok, det = enviar_whatsapp_texto(numero_raw, body)
        log_envio_wa(etiqueta, numero_raw, ok, det)
        return ok, det, ok

    tpl_cfg = _template_alerta_config()
    if tpl_cfg:
        ok, det = enviar_plantilla_alerta(numero_raw, body)
        log_envio_wa(f"{etiqueta} plantilla alerta", numero_raw, ok, det)
        if ok:
            return ok, det, ok

    encolar_alerta(numero_raw, body, etiqueta=etiqueta, origen=origen)
    if not _plantilla_reciente(numero_raw):
        ok_tpl, det_tpl = enviar_plantilla_bienvenida(numero_raw)
        log_envio_wa(f"{etiqueta} plantilla (fuera 24h)", numero_raw, ok_tpl, det_tpl)
        if ok_tpl:
            _marcar_plantilla_enviada(numero_raw)
    n = len(listar_alertas_pendientes(numero_raw))
    det = (
        f"encolado ({n} pendiente(s)); responde al +593 96 279 3109 para recibir contenido"
    )
    log_envio_wa(f"{etiqueta} cola", numero_raw, True, det)
    return True, det, False


def entregar_alertas_pendientes_sync(numero: str, *, pausa_sec: float = 1.2) -> int:
    """Entrega cola por texto libre (ventana 24h recién abierta)."""
    from alertas_tatami import enviar_whatsapp_texto, log_envio_wa

    wa_id = _solo_digitos(numero)
    if not wa_id or not usuario_en_ventana_24h(wa_id):
        return 0
    items = listar_alertas_pendientes(wa_id)
    if not items:
        return 0
    entregados = 0
    ids_ok: list[str] = []
    for it in items:
        cuerpo = (it.get("cuerpo") or "").strip()
        if not cuerpo:
            continue
        ok, det = enviar_whatsapp_texto(wa_id, cuerpo)
        lab = it.get("etiqueta") or "pendiente"
        log_envio_wa(f"{lab} entrega cola", wa_id, ok, det)
        if not ok:
            break
        entregados += 1
        if it.get("id"):
            ids_ok.append(str(it["id"]))
        time.sleep(pausa_sec)
    if ids_ok:
        marcar_entregadas(ids_ok)
        if entregados == len(items):
            if _supabase_storage_ok() and any(str(i).startswith("st-") for i in ids_ok):
                _limpiar_pendientes_storage(wa_id)
            elif all(str(i).startswith("local-") for i in ids_ok):
                _limpiar_pendientes_local(wa_id)
    if entregados:
        print(f"  WA cola entregada a …{wa_id[-4:]}: {entregados} mensaje(s)")
    return entregados


async def entregar_alertas_pendientes_async(wa_id: str) -> int:
    """Versión async para whatsapp_webhook."""
    import asyncio

    return await asyncio.to_thread(entregar_alertas_pendientes_sync, wa_id)

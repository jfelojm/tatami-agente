from __future__ import annotations

import os

import json
import anthropic
import httpx
from dotenv import load_dotenv

# PyMuPDF (pymupdf) para extraer texto de PDF
try:
    import fitz  # type: ignore
except Exception:
    fitz = None

load_dotenv()  # ← debe estar antes de create_client

from supabase import create_client

from sesiones_factura import cerrar_sesion, crear_sesion_confirmacion, leer_sesion


supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _vision_extraer_factura(texto_pdf: str) -> dict:
    """
    Extrae una factura estructurada desde el texto del PDF usando el modelo.
    Devuelve dict con keys esperadas por _aplicar_factura_vision().
    """
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")

    prompt = (
        "Extrae una factura desde texto de PDF. Devuelve SOLO JSON válido, sin markdown.\n"
        "Schema:\n"
        "{\n"
        '  \"ruc_proveedor\": \"string\",\n'
        '  \"num_factura\": \"001-002-000000123\",\n'
        '  \"fecha_emision\": \"YYYY-MM-DD\",\n'
        '  \"nombre_proveedor\": \"string\",\n'
        '  \"subtotal_sin_iva\": number,\n'
        '  \"iva\": number,\n'
        '  \"total\": number,\n'
        '  \"items\": [\n'
        "    {\n"
        '      \"descripcion\": \"string\",\n'
        '      \"cantidad\": number,\n'
        '      \"precio_unitario\": number,\n'
        '      \"precio_total\": number\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Reglas:\n"
        "- Si falta IVA/subtotal, usa 0.\n"
        "- cantidad/precio_unitario/precio_total deben ser números.\n"
        "- No inventes items.\n\n"
        "TEXTO PDF:\n"
        + (texto_pdf or "")[:12000]
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1400,
        messages=[{"role": "user", "content": prompt}],
    )

    txt = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            txt += block.text
    txt = (txt or "").strip()
    if not txt:
        raise RuntimeError("Respuesta vacía extrayendo factura.")
    return json.loads(txt)


def _vision_extraer_factura_imagen(imagen_bytes: bytes, mime_type: str) -> dict:
    """
    Envía la imagen directamente a Claude Vision y retorna
    el factura_dict estructurado (mismos campos que _vision_extraer_factura).
    """
    import base64
    import anthropic
    import json
    import os

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    img_b64 = base64.standard_b64encode(imagen_bytes).decode("utf-8")

    SYSTEM = """Eres un extractor de facturas comerciales ecuatorianas.
Responde SOLO con JSON válido, sin texto adicional, sin markdown.
Formato exacto:
{
  "ruc_proveedor": "string o null",
  "nombre_proveedor": "string o null",
  "num_factura": "001-002-000XXXXXX o null",
  "fecha_emision": "YYYY-MM-DD o null",
  "items": [
    {
      "descripcion": "string",
      "cantidad": número o null,
      "precio_unitario": número o null,
      "precio_total": número o null,
      "confianza": "ALTA | MEDIA | BAJA"
    }
  ],
  "subtotal_sin_iva": número o null,
  "iva": número o null,
  "total": número o null
}
NUNCA inventes valores. Si no puedes leer un campo usa null.
confianza BAJA si el valor es dudoso."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": "Extrae todos los datos de esta factura."},
                ],
            }
        ],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def _formatear_resumen_confirmacion(factura_dict: dict) -> str:
    def _to_f(v) -> float:
        try:
            return float(v)
        except Exception:
            try:
                return float(str(v).replace(",", "."))
            except Exception:
                return 0.0

    proveedor = factura_dict.get("nombre_proveedor") or "?"
    ruc = factura_dict.get("ruc_proveedor") or "?"
    num = factura_dict.get("num_factura") or "?"
    fecha = factura_dict.get("fecha_emision") or "?"
    items = factura_dict.get("items") or []
    total = factura_dict.get("total") or factura_dict.get("subtotal_sin_iva") or 0

    lineas = [
        "🧾 *Factura detectada*",
        f"Proveedor: {proveedor}",
        f"RUC: {ruc}",
        f"Nro: {num}",
        f"Fecha: {fecha}",
        "──────────────────",
    ]

    items_con_datos = []
    items_sin_datos = []

    for item in items:
        desc = item.get("descripcion") or item.get("descripcion_proveedor") or "?"
        cant = item.get("cantidad")
        price = item.get("precio_total") or item.get("precio_total_sin_impuesto")
        conf = item.get("confianza", "ALTA")

        if cant and price:
            flag = " ⚠️" if conf in ("BAJA", "MEDIA") else ""
            lineas.append(f"• {desc}{flag} — {cant} × ${_to_f(price):.2f}")
            items_con_datos.append(desc)
        else:
            items_sin_datos.append(desc)

    if items_sin_datos:
        lineas.append(f"  _(sin datos: {', '.join(items_sin_datos)})_")

    lineas += [
        "──────────────────",
        f"Total: ${_to_f(total):.2f}",
        "",
        f"Se registrarán *{len(items_con_datos)} ítem(s)* en inventario.",
    ]

    if items_sin_datos:
        lineas.append(
            f"⚠️ {len(items_sin_datos)} ítem(s) sin cantidad/precio — no se registrarán."
        )

    lineas += [
        "",
        "¿Confirmas? Responde *SI* para aplicar o *NO* para cancelar.",
    ]

    return "\n".join(lineas)


async def _aplicar_factura_vision(factura_dict: dict, from_number: str) -> str:
    from procesar_facturas_drive import (
        procesar_factura_dict,
        registrar_factura_procesada,
        factura_ya_procesada,
    )

    ruc = factura_dict["ruc_proveedor"]
    num = factura_dict["num_factura"]
    fecha = factura_dict["fecha_emision"]

    # Construir el dict en el formato que espera procesar_factura_dict
    # (misma estructura que retorna parsear_xml_sri)
    factura = {
        "num_factura": num,
        "ruc": ruc,
        "fecha_emision": fecha,  # se conserva por compatibilidad (aunque el flujo usa fecha_factura)
        "fecha_factura": fecha,
        "razon_social": factura_dict.get("nombre_proveedor", ""),
        "total_sin_impuesto": factura_dict.get("subtotal_sin_iva", 0),
        "forma_pago": "",
        "_archivo_drive": None,  # no viene de Drive
        "items": [
            {
                # Vision no tiene cod_item_xml — usamos descripcion como clave
                "cod_item_xml": item.get("descripcion", ""),
                "descripcion_proveedor": item.get("descripcion", ""),
                "cantidad": item.get("cantidad", 0),
                "precio_unitario_xml": item.get("precio_unitario", 0),
                "descuento": 0,
                "precio_total_sin_impuesto": item.get("precio_total", 0),
                # costo_efectivo = precio_total / cantidad (igual que XML)
                "costo_efectivo": (
                    item["precio_total"] / item["cantidad"]
                    if item.get("cantidad") and item["cantidad"] > 0
                    else item.get("precio_unitario", 0)
                ),
            }
            for item in factura_dict.get("items", [])
            if item.get("descripcion") and item.get("cantidad")
        ],
    }

    # Si ya estaba COMPLETA en facturas_procesadas, antes se salía sin ejecutar nada:
    # eso impedía re-sincronizar precios en BD_ITEMS_PROV tras un fix o catálogo nuevo.
    ya_completa_en_db = factura_ya_procesada(num, ruc)

    # Siempre: mov duplicado se omite solo; precios se vuelven a escribir por línea matcheada.
    resultado = procesar_factura_dict(factura, dry_run=False, origen="VISION")

    items_warn = len(resultado.get("sin_match") or []) + len(resultado.get("warn") or [])
    registrar_factura_procesada(
        factura=factura,
        archivo={"id": "", "name": f"VISION:{from_number}"},
        items_matcheados=int(resultado.get("matcheados") or 0),
        items_warn=items_warn,
        dry_run=False,
    )

    # Respuesta WhatsApp
    if ya_completa_en_db:
        lineas = [
            f"ℹ️ Factura {num} ya estaba *COMPLETA* en el sistema.",
            f"Se *re-sincronizaron precios* en catálogo (estado corrida: {resultado['estado']}).",
            f"{resultado['matcheados']} ítem(s) con match revisados.",
        ]
    else:
        lineas = [f"✅ Factura {num} registrada — {resultado['estado']}"]
        lineas.append(f"{resultado['matcheados']} ítem(s) aplicados en inventario.")

    if resultado.get("sin_match"):
        lineas.append(
            f"⚠️ {len(resultado['sin_match'])} ítem(s) sin match:\n"
            + "\n".join(f"  - {d}" for d in resultado["sin_match"])
            + "\nMoisés será notificado."
        )
    if resultado.get("warn"):
        lineas.append(f"ℹ️ {len(resultado['warn'])} advertencia(s) — ver logs.")

    return "\n".join(lineas)


async def _descargar_media_meta(media_id: str) -> tuple[bytes | None, str, str]:
    """Descarga media desde WhatsApp Cloud API. Retorna (bytes, mime_type, filename)."""
    media_id = (media_id or "").strip()
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not media_id or not token:
        return None, "", ""
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(
                f"https://graph.facebook.com/v25.0/{media_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code >= 400:
                return None, "", ""
            info = r.json() or {}
            url = (info.get("url") or "").strip()
            mime = (info.get("mime_type") or "").strip()
            filename = (info.get("filename") or "").strip()
            if not url:
                return None, mime, filename

            r2 = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if r2.status_code >= 400:
                return None, mime, filename
            return r2.content, mime, filename
    except Exception:
        return None, "", ""


def _extraer_texto_pdf(pdf_bytes: bytes, *, max_chars: int = 12000) -> str:
    if not pdf_bytes or not fitz:
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts: list[str] = []
        for i in range(min(15, doc.page_count)):
            t = (doc.load_page(i).get_text("text") or "").strip()
            if t:
                parts.append(t)
            if sum(len(x) for x in parts) >= max_chars:
                break
        return ("\n\n".join(parts).strip())[:max_chars]
    except Exception:
        return ""


async def handle_mensaje_media(message: dict, from_number: str) -> str:
    """
    Procesa 'document' y 'image'. En esta primera versión:
    - document PDF: extrae texto y abre sesión de confirmación (placeholder).
    - image / no-PDF: indica que falta OCR/soporte.
    """
    msg_type = (message.get("type") or "").strip()
    media_id = ""
    mime = ""
    filename = ""

    if msg_type == "document":
        d = message.get("document", {}) or {}
        media_id = (d.get("id") or "").strip()
        mime = (d.get("mime_type") or "").strip()
        filename = (d.get("filename") or "").strip()
    elif msg_type == "image":
        d = message.get("image", {}) or {}
        media_id = (d.get("id") or "").strip()
        mime = (d.get("mime_type") or "").strip()
    else:
        return "Por ahora solo proceso documentos PDF e imágenes."

    blob, mime2, fn2 = await _descargar_media_meta(media_id)
    if mime2:
        mime = mime2
    if fn2:
        filename = fn2

    if not blob:
        return "No pude descargar el archivo desde WhatsApp. Reintenta enviándolo otra vez."

    mime_l = (mime or "").lower().strip()
    # Detectar XML por mime_type o por nombre del archivo (llegan como document)
    es_xml = ("xml" in mime_l) or (filename or "").lower().endswith(".xml")

    if es_xml:
        try:
            texto_xml = blob.decode("utf-8", errors="replace")
        except Exception as e:
            return f"No pude leer el XML: {e}"

        try:
            from procesar_facturas_drive import factura_ya_procesada, parsear_xml_sri
        except Exception as e:
            return f"Error importando parser XML: {e}"

        try:
            factura_parsed = parsear_xml_sri(texto_xml)
        except Exception as e:
            return f"Error parseando XML: {e}"

        if not factura_parsed:
            return "No pude parsear este XML. ¿Es una factura electrónica SRI Ecuador?"

        # Deduplicación (si ya fue registrada en facturas_procesadas COMPLETA)
        try:
            if factura_ya_procesada(
                str(factura_parsed.get("num_factura") or "").strip(),
                str(factura_parsed.get("ruc") or "").strip(),
            ):
                return (
                    "⚠️ Esta factura ya estaba registrada y no se aplicaron cambios.\n"
                    f"Nro: {factura_parsed.get('num_factura','')}"
                )
        except Exception:
            # Si falla la consulta, seguimos (se deduplicará por mov_inventario al aplicar)
            pass

        # Convertir al formato factura_dict que usa el resto del flujo
        factura_dict = {
            "ruc_proveedor": factura_parsed.get("ruc", "") or "",
            "nombre_proveedor": factura_parsed.get("razon_social", "") or "",
            "num_factura": factura_parsed.get("num_factura", "") or "",
            "fecha_emision": factura_parsed.get("fecha_factura", "") or "",
            "subtotal_sin_iva": factura_parsed.get("total_sin_impuesto", 0) or 0,
            "iva": 0,
            "total": factura_parsed.get("total_sin_impuesto", 0) or 0,
            "items": [
                {
                    "descripcion": i.get("descripcion_proveedor", "") or "",
                    "cantidad": i.get("cantidad", 0) or 0,
                    "precio_unitario": i.get("precio_unitario_xml", 0) or 0,
                    "precio_total": i.get("precio_total_sin_impuesto", 0) or 0,
                    "confianza": "ALTA",  # XML es preciso
                }
                for i in (factura_parsed.get("items") or [])
            ],
            "_origen": "XML_WA",
        }

        texto_pdf = ""
        # Guardar sesión con factura estructurada para confirmación (misma lógica que PDF/imagen)
        crear_sesion_confirmacion(
            from_number,
            payload={
                "tipo": "factura_media",
                "filename": filename,
                "mime": mime,
                "texto_pdf": texto_pdf,
                "factura_dict": factura_dict,
            },
            ttl_min=30,
        )
        return _formatear_resumen_confirmacion(factura_dict)

    is_pdf = (mime_l == "application/pdf") or (filename or "").lower().endswith(".pdf") or ("pdf" in mime_l)

    texto_pdf = ""
    factura_dict = None

    if is_pdf:
        # Flujo actual con PyMuPDF — no cambiar (bytes -> texto)
        if not fitz:
            return "Recibí el PDF, pero falta PyMuPDF (pymupdf) en el servidor para leerlo."

        texto_pdf = _extraer_texto_pdf(blob)
        if not texto_pdf:
            # PDF escaneado — renderizar primera página y usar Vision imagen
            try:
                import fitz

                doc = fitz.open(stream=blob, filetype="pdf")
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                doc.close()
                png_bytes = pix.tobytes("png")
                factura_dict = _vision_extraer_factura_imagen(png_bytes, "image/png")
            except Exception as e:
                import logging

                logger = logging.getLogger(__name__)
                logger.error("Fallback PDF escaneado falló: %s", e)
                return (
                    "No pude leer este PDF. Intenta:\n"
                    "• Enviar una foto de la factura directamente\n"
                    "• O un PDF con texto seleccionable"
                )

        # Texto -> Vision
        try:
            factura_dict = _vision_extraer_factura(texto_pdf)
        except Exception:
            return (
                "No pude leer la factura. Intenta:\n"
                "• Foto más cerca y con buena luz\n"
                "• Imagen sin sombras ni reflejos\n"
                "• O envía el PDF original del proveedor"
            )
    else:
        # Imagen directa a Vision (bytes)
        if not mime_l.startswith("image/"):
            return f"Recibí el documento ({mime or 'sin mime'}), pero por ahora solo proceso PDFs o imágenes."

        # HEIC/HEIF: tratar como jpeg (Meta suele convertir antes de entregarlo)
        media_type = mime_l
        if media_type in ("image/heic", "image/heif"):
            media_type = "image/jpeg"

        try:
            factura_dict = _vision_extraer_factura_imagen(blob, media_type)
        except Exception:
            return (
                "No pude leer la factura. Intenta:\n"
                "• Foto más cerca y con buena luz\n"
                "• Imagen sin sombras ni reflejos\n"
                "• O envía el PDF original del proveedor"
            )

    # el resto del flujo (guardar sesión, resumen, etc.) es idéntico para PDF e imagen
    crear_sesion_confirmacion(
        from_number,
        payload={
            "tipo": "factura_media",
            "filename": filename,
            "mime": mime,
            "texto_pdf": texto_pdf,
            "factura_dict": factura_dict,
        },
        ttl_min=30,
    )

    return _formatear_resumen_confirmacion(factura_dict)


async def handle_confirmacion(texto: str, from_number: str) -> str:
    """
    Maneja confirmaciones SI/NO/CANCELAR cuando hay sesión activa.
    Por ahora solo cierra sesión; luego puede ejecutar un pipeline de registro.
    """
    t = (texto or "").strip().upper()
    ses = leer_sesion(from_number)
    if not ses:
        return "No tengo ninguna operación pendiente para confirmar."

    if t in ("NO", "CANCELAR"):
        cerrar_sesion(from_number)
        return "Listo. Cancelado."

    if t in ("SI", "SÍ"):
        payload = ses.payload or {}
        factura_dict = payload.get("factura_dict")
        if not isinstance(factura_dict, dict):
            cerrar_sesion(from_number)
            return "No encontré la factura estructurada en la sesión. Reenvía el PDF por favor."
        try:
            resp = await _aplicar_factura_vision(factura_dict, from_number)
        finally:
            cerrar_sesion(from_number)
        return resp

    return "Respuesta inválida. Usa SI / NO / CANCELAR."


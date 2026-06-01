"""
Alertas de inventario para MPs de proveedores de barra (BD_PROV.Tipo = Barra).

Variables (.env):
  TATAMI_ALERT_INVENTARIO_BARRA=1        Activa avisos WA de bajo PAR y stock negativo (barra)
  ALERTA_WA_INVENTARIO_BARRA             Destino mientras revisás (prioridad sobre Eduardo)
  ALERTA_WA_EDUARDO                      Producción (solo si no hay ALERTA_WA_INVENTARIO_BARRA)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(override=True)

BODEGAS_BARRA_ALERTA = frozenset({"BOD-002", "BOD-003"})


def alertas_inventario_barra_habilitadas() -> bool:
    v = (os.getenv("TATAMI_ALERT_INVENTARIO_BARRA") or "").strip().lower()
    return v in ("1", "true", "yes", "si", "sí")


def destinatario_inventario_barra() -> tuple[str, str]:
    """
    (numero, etiqueta_log). Preview: ALERTA_WA_INVENTARIO_BARRA; prod: ALERTA_WA_EDUARDO.
    Nunca envía a Moisés/Felipe salvo que los pongas explícitamente en ALERTA_WA_INVENTARIO_BARRA.
    """
    preview = (os.getenv("ALERTA_WA_INVENTARIO_BARRA") or "").strip()
    if preview:
        return preview, "preview inventario barra"
    eduardo = (os.getenv("ALERTA_WA_EDUARDO") or "").strip()
    if eduardo:
        return eduardo, "Eduardo inventario barra"
    return "", ""


def _norm_cod_mp(cod: object) -> str:
    s = str(cod or "").strip()
    return s.zfill(3) if s else ""


def _norm_cod_prov(cod: object) -> str:
    s = str(cod or "").strip()
    return s.zfill(3) if s.isdigit() else s


def _find_header(values: list[list[str]], key: str) -> tuple[int, list[str]] | None:
    for i, row in enumerate(values):
        headers = [c.strip() for c in row]
        if key in headers:
            return i, headers
    return None


def _row_dict(headers: list[str], row: list[str]) -> dict[str, str]:
    return {headers[j]: (row[j].strip() if j < len(row) else "") for j in range(len(headers))}


def cargar_proveedores_barra() -> dict[str, str]:
    """cod_proveedor (normalizado) -> razon_social para Tipo Barra en BD_PROV."""
    from procesar_facturas_drive import _get_sheet

    sh = _get_sheet()
    vals = sh.worksheet("BD_PROV").get_all_values()
    found = _find_header(vals, "cod_proveedor")
    if not found:
        return {}
    hi, headers = found
    out: dict[str, str] = {}
    for row in vals[hi + 1 :]:
        if not any(c.strip() for c in row):
            continue
        r = _row_dict(headers, row)
        cod = _norm_cod_prov(r.get("cod_proveedor", ""))
        if not cod:
            continue
        tipo = (r.get("Tipo") or "").strip().upper()
        if tipo == "BARRA":
            out[cod] = (r.get("razon_social") or cod).strip()
    return out


def cargar_cod_mps_proveedores_barra() -> set[str]:
    """
    MPs del catálogo (BD_ITEMS_PROV) cuyo proveedor está marcado Barra en BD_PROV.
    """
    provs = cargar_proveedores_barra()
    if not provs:
        return set()

    from procesar_facturas_drive import cargar_bd_items_prov

    mps: set[str] = set()
    for it in cargar_bd_items_prov():
        cp = _norm_cod_prov(it.get("cod_proveedor"))
        if cp not in provs:
            continue
        cod = _norm_cod_mp(it.get("cod_mp_sistema"))
        if cod:
            mps.add(cod)
    return mps


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or default)
    except (TypeError, ValueError):
        return default


def evaluar_bajo_par_barra() -> list[dict]:
    """
    MPs de proveedores barra con fila BOD-002 bajo par_level global.
    """
    from whatsapp_webhook import leer_bd_mp_sistema

    mps_barra = cargar_cod_mps_proveedores_barra()
    if not mps_barra:
        return []

    out: list[dict] = []
    for r in leer_bd_mp_sistema():
        cod = _norm_cod_mp(r.get("cod_mp_sistema"))
        bod = (r.get("cod_bodega") or "").strip().upper()
        if cod not in mps_barra or bod != "BOD-002":
            continue
        stock = _to_float(r.get("stock_actual"))
        par = _to_float(r.get("par_level"))
        if par <= 0 or stock >= par:
            continue
        out.append(
            {
                "cod_mp": cod,
                "nombre_mp": (r.get("nombre_mp") or cod).strip(),
                "cod_bodega": bod,
                "stock_actual": round(stock, 4),
                "par_level": round(par, 4),
                "unidad": (r.get("unidad_base") or "").strip(),
                "deficit_pct": round((1 - stock / par) * 100, 1) if par else 0.0,
            }
        )
    out.sort(key=lambda x: x["deficit_pct"], reverse=True)
    return out


def evaluar_negativos_barra() -> list[dict]:
    """Stock negativo en BOD-002/BOD-003 para MPs de proveedores barra."""
    from whatsapp_webhook import leer_bd_mp_sistema

    mps_barra = cargar_cod_mps_proveedores_barra()
    if not mps_barra:
        return []

    out: list[dict] = []
    for r in leer_bd_mp_sistema():
        cod = _norm_cod_mp(r.get("cod_mp_sistema"))
        bod = (r.get("cod_bodega") or "").strip().upper()
        if cod not in mps_barra or bod not in BODEGAS_BARRA_ALERTA:
            continue
        stock = _to_float(r.get("stock_actual"))
        if stock >= -0.0001:
            continue
        out.append(
            {
                "cod_mp": cod,
                "nombre_mp": (r.get("nombre_mp") or cod).strip(),
                "cod_bodega": bod,
                "stock_actual": round(stock, 4),
                "unidad": (r.get("unidad_base") or "").strip(),
            }
        )
    out.sort(key=lambda x: x["stock_actual"])
    return out


def _formatear_lineas(items: list[dict], *, tipo: str) -> str:
    lineas = []
    for it in items[:25]:
        if tipo == "par":
            lineas.append(
                f"• {it['cod_mp']} {it['nombre_mp'][:38]} @ {it['cod_bodega']}: "
                f"stock {it['stock_actual']} / PAR {it['par_level']} {it['unidad']} "
                f"({it['deficit_pct']}% bajo)"
            )
        else:
            lineas.append(
                f"• {it['cod_mp']} {it['nombre_mp'][:38]} @ {it['cod_bodega']}: "
                f"stock {it['stock_actual']} {it['unidad']}"
            )
    extra = f"\n… y {len(items) - 25} más" if len(items) > 25 else ""
    return "\n".join(lineas) + extra


def enviar_alertas_inventario_barra(*, origen: str = "pipeline") -> dict:
    """
    Envía WA a ALERTA_WA_EDUARDO si hay bajo PAR o negativos (MPs proveedores barra).
    Retorna resumen {bajo_par, negativos, enviado}.
    """
    res = {"bajo_par": 0, "negativos": 0, "enviado": False, "omitido": None}
    if not alertas_inventario_barra_habilitadas():
        res["omitido"] = "TATAMI_ALERT_INVENTARIO_BARRA no activo"
        return res

    numero, etiqueta = destinatario_inventario_barra()
    if not numero:
        res["omitido"] = "ALERTA_WA_INVENTARIO_BARRA o ALERTA_WA_EDUARDO vacío en .env"
        print("  WA [OMITIDO] inventario barra: sin destinatario configurado")
        return res

    bajo_par = evaluar_bajo_par_barra()
    negativos = evaluar_negativos_barra()
    res["bajo_par"] = len(bajo_par)
    res["negativos"] = len(negativos)

    if not bajo_par and not negativos:
        res["omitido"] = "sin alertas"
        return res

    from alertas_tatami import enviar_alerta, enviar_whatsapp_texto, log_envio_wa

    bloques = [f"📊 Inventario barra ({origen}) — proveedores Tipo=Barra"]
    if (os.getenv("ALERTA_WA_INVENTARIO_BARRA") or "").strip():
        bloques[0] += " [preview — revisión]"
    if bajo_par:
        bloques.append(f"\n⚠ Bajo PAR ({len(bajo_par)}):\n" + _formatear_lineas(bajo_par, tipo="par"))
    if negativos:
        bloques.append(
            f"\n⚠ Stock negativo ({len(negativos)}):\n" + _formatear_lineas(negativos, tipo="neg")
        )
    cuerpo = "\n".join(bloques).strip()[:4096]

    enviar_alerta("Inventario barra", cuerpo, estado="WARN")
    ok, msg = enviar_whatsapp_texto(numero, cuerpo)
    log_envio_wa(etiqueta, numero, ok, msg)
    res["enviado"] = ok
    if not ok:
        res["omitido"] = msg
    return res


def filtrar_descargo_negativos_barra(items: list[dict]) -> list[dict]:
    """Subconjunto de alertas post-descargo: MPs proveedor barra en BOD-002/003."""
    mps = cargar_cod_mps_proveedores_barra()
    if not mps:
        return []
    out = []
    for it in items:
        cod = _norm_cod_mp(it.get("cod_mp"))
        bod = (it.get("cod_bodega") or "").strip().upper()
        if cod in mps and bod in BODEGAS_BARRA_ALERTA:
            out.append(it)
    return out


def alerta_wa_descargo_stock_negativo_barra(items: list[dict]) -> None:
    """Tras descargo: aviso inmediato a Eduardo solo MPs proveedores barra."""
    if not items or not alertas_inventario_barra_habilitadas():
        return
    filtrados = filtrar_descargo_negativos_barra(items)
    if not filtrados:
        return

    numero, etiqueta = destinatario_inventario_barra()
    if not numero:
        print("  WA [OMITIDO] descargo barra: sin destinatario configurado")
        return

    from alertas_tatami import enviar_alerta, enviar_whatsapp_texto, log_envio_wa

    lineas = []
    for it in filtrados[:25]:
        lineas.append(
            f"• {it.get('cod_mp')} {it.get('nombre_mp', '')[:38]} @ {it.get('cod_bodega')}: "
            f"stock {it.get('stock_restante')} {it.get('unidad', '')} "
            f"(venta {it.get('cod_venta', '')})"
        )
    extra = f"\n… y {len(filtrados) - 25} más" if len(filtrados) > 25 else ""
    titulo = "⚠ Descargo — stock negativo (proveedores barra)"
    if (os.getenv("ALERTA_WA_INVENTARIO_BARRA") or "").strip():
        titulo += " [preview]"
    cuerpo = titulo + "\n" + "\n".join(lineas) + extra
    enviar_alerta("Descargo stock negativo barra", cuerpo, estado="WARN")
    ok, msg = enviar_whatsapp_texto(numero, cuerpo)
    log_envio_wa(etiqueta + " descargo", numero, ok, msg)

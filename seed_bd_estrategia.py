"""
Pobla BD_CONFIG (claves máquina) y BD_ESTRATEGIA (matrices editables) en el Spreadsheet maestro.

Uso:
  python seed_bd_estrategia.py
  python seed_bd_estrategia.py --force   # sobrescribe filas estrategia_* / sched_* / perm_* / pipe_* / alert_* / area_* / rol_*

No borra claves legacy (umbral_alerta_precio, smartmenu_*, etc.).
"""

from __future__ import annotations

import argparse
import os
import sys

import gspread
from dotenv import load_dotenv
from google_credentials import google_credentials, has_google_credentials
load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

HEADERS_CONFIG = ["clave", "valor", "descripcion", "tipo"]

# Prefijos que --force puede reemplazar en BD_CONFIG
ESTRATEGIA_PREFIXES = (
    "estrategia_",
    "sched_",
    "rol_",
    "perm_",
    "pipe_",
    "alert_",
    "area_",
    "digest_",
    "par_modo",
    "par_cron_",
    "par_script",
    "par_reemplaza_",
    "par_post_",
    "par_wa",
)

BD_CONFIG_ROWS: list[list[str]] = [
    # --- A Metadatos ---
    ["estrategia_version", "2026-06-12-v1", "Versión matriz permisos/pipelines/alertas", "string"],
    ["estrategia_zona_horaria", "America/Guayaquil", "Zona horaria cron", "string"],
    [
        "estrategia_notas",
        "Horario secuencial 7-00 reemplaza cuadrante. PAR domingo 20:00. Pedidos cocina off hasta gestión inventario.",
        "Notas operativas",
        "string",
    ],
    # --- B Scheduling ---
    ["sched_modo", "horario_secuencial", "horario_secuencial | cuadrante_legacy", "string"],
    ["sched_hora_inicio", "7", "Primera corrida (hora local)", "int"],
    ["sched_hora_fin", "0", "Última corrida (0=medianoche)", "int"],
    ["sched_intervalo_min", "60", "Minutos entre corridas", "int"],
    [
        "sched_orden_pipelines",
        "ventas_reconciliar,descargo,facturas_sri,carga_inventario_mp",
        "Orden estricto en cada hora",
        "csv",
    ],
    ["sched_si_falla_paso", "continuar_con_warn", "continuar_con_warn | abortar_cadena | abortar_solo_paso", "string"],
    ["sched_escalacion_fallo_roles", "ADMIN", "WA si pipeline falla", "csv"],
    ["sched_legacy_cuadrante_activo", "false", "Desactivar TatamiCuadrante_* al migrar", "bool"],
    # --- C Roles (.env refs; teléfonos en BD_ESTRATEGIA o .env) ---
    [
        "rol_catalogo",
        "ADMIN,SOCIO,ADMIN_COMPRAS,JEFE_BARRA,JEFE_COCINA,STAFF_BARRA,STAFF_COCINA,OPS_ALERTAS",
        "Roles válidos",
        "csv",
    ],
    ["rol_ADMIN_env_wa", "ALERTA_WA_FELIPE", "Felipe control total", "string"],
    ["rol_SOCIO_env_allowlist", "ALLOWLIST_SOCIO", "Socios", "string"],
    ["rol_ADMIN_COMPRAS_env_wa", "ALERTA_WA_MARY", "Mary admin compras", "string"],
    ["rol_ADMIN_COMPRAS_env_allowlist", "ALLOWLIST_ADMIN_COMPRAS", "Mary chat", "string"],
    ["rol_JEFE_BARRA_env_wa", "ALERTA_WA_EDUARDO", "Eduardo jefe barra", "string"],
    ["rol_JEFE_BARRA_env_allowlist", "ALLOWLIST_JEFE_BARRA", "Eduardo chat", "string"],
    ["rol_JEFE_COCINA_env_wa", "ALERTA_WA_JACKY", "Jacky jefe cocina", "string"],
    ["rol_JEFE_COCINA_env_allowlist", "ALLOWLIST_JEFE_COCINA", "Jacky chat", "string"],
    ["rol_STAFF_BARRA_env_allowlist", "ALLOWLIST_STAFF_BARRA", "Personal barra sin costos", "string"],
    ["rol_STAFF_COCINA_env_allowlist", "ALLOWLIST_STAFF_COCINA", "Personal cocina sin costos", "string"],
    ["rol_OPS_ALERTAS_env_wa_moises", "ALERTA_WA_MOISES", "Moisés alertas ops", "string"],
    ["rol_OPS_ALERTAS_env_wa_israel", "ALERTA_WA_ISRAEL", "Israel alertas ops", "string"],
    # --- D Permisos chat ---
    [
        "perm_ver_costos_roles",
        "ADMIN,SOCIO,ADMIN_COMPRAS,JEFE_BARRA,JEFE_COCINA",
        "Roles que ven USD/costos",
        "csv",
    ],
    [
        "perm_ver_costos_tools",
        "costo_plato,receta_ingredientes,costo_subreceta,inventario_valorizado,inventario_por_bodega,compras_facturas_rango,compras_factura_detalle,auditar_costos_recetas,resumen_operativo_hoy",
        "Tools bloqueadas sin perm_ver_costos",
        "csv",
    ],
    [
        "perm_receta_sin_costos_roles",
        "STAFF_BARRA,STAFF_COCINA,JEFE_BARRA,JEFE_COCINA,ADMIN,SOCIO,ADMIN_COMPRAS",
        "Recetas/ingredientes sin USD",
        "csv",
    ],
    ["perm_receta_sin_costos_tool", "receta_sin_costo", "Tool futura sin USD", "string"],
    [
        "perm_inventario_consulta_roles",
        "ADMIN,SOCIO,ADMIN_COMPRAS,JEFE_BARRA,STAFF_BARRA",
        "Stock, PAR, negativos",
        "csv",
    ],
    [
        "perm_inventario_consulta_tools",
        "stock_critico,stocks_negativos,stock_ingrediente,bodega_producto,mp_incompletas,kardex",
        "Tools inventario lectura",
        "csv",
    ],
    ["perm_conteo_iniciar_roles", "ADMIN,JEFE_BARRA,JEFE_COCINA,ADMIN_COMPRAS", "INICIAR CONTEO", "csv"],
    ["perm_conteo_aprobar_roles", "ADMIN,JEFE_BARRA,JEFE_COCINA", "APROBAR conteo", "csv"],
    ["perm_conteo_bodegas_JEFE_BARRA", "BOD-002,BOD-003", "Bodegas Eduardo", "csv"],
    ["perm_conteo_bodegas_JEFE_COCINA", "BOD-001,BOD-005", "Bodegas Jacky", "csv"],
    ["perm_traslado_roles", "ADMIN,JEFE_BARRA,JEFE_COCINA,STAFF_COCINA", "trasladar_mp", "csv"],
    ["perm_producir_sub_roles", "ADMIN,JEFE_BARRA,JEFE_COCINA,STAFF_BARRA,STAFF_COCINA", "PRODUCIR SUB", "csv"],
    ["perm_producir_sub_bodegas_STAFF_BARRA", "BOD-002", "Staff barra batches", "csv"],
    ["perm_producir_sub_bodegas_STAFF_COCINA", "BOD-001", "Staff cocina batches", "csv"],
    [
        "perm_ventas_consulta_roles",
        "ADMIN,SOCIO,ADMIN_COMPRAS,JEFE_BARRA,STAFF_BARRA",
        "Consulta ventas",
        "csv",
    ],
    ["perm_escritura_total_roles", "ADMIN", "Escritura total", "csv"],
    ["perm_socio_escritura", "false", "Socios solo lectura", "bool"],
    ["perm_ops_alertas_roles", "OPS_ALERTAS", "Rol auxiliar alertas 8am (dual con SOCIO)", "csv"],
    # --- E Pipelines horarios ---
    ["pipe_ventas_reconciliar_activo", "true", "Paso 1 cada hora", "bool"],
    ["pipe_ventas_reconciliar_script", "ventas_smartmenu.py,reconciliar_ventas_dia.py", "Scripts paso 1", "csv"],
    ["pipe_ventas_reconciliar_fecha", "ayer_hasta_hora", "ayer_hasta_hora | hoy | ayer_completo", "string"],
    ["pipe_ventas_reconciliar_wa", "nadie", "WA si OK", "string"],
    ["pipe_descargo_activo", "true", "Paso 2", "bool"],
    ["pipe_descargo_script", "descargo_inventario.py", "Descargo MP+SUB", "string"],
    ["pipe_descargo_subrecetas", "true", "Requiere DESCARGO_SUBRECETAS=1 en .env", "bool"],
    ["pipe_descargo_wa", "nadie", "WA si OK", "string"],
    ["pipe_descargo_wa_stock_negativo", "inmediato_digest", "inmediato_digest | solo_8am | nadie", "string"],
    ["pipe_facturas_sri_activo", "true", "Paso 3", "bool"],
    ["pipe_facturas_sri_modo", "solo_proceso_cola", "descarga_y_proceso | solo_proceso_cola", "string"],
    [
        "pipe_facturas_sri_horas_descarga",
        "",
        "Horas pipeline con descarga portal (vacío = usar tareas AM/PM)",
        "csv",
    ],
    ["pipe_facturas_sri_wa_ok", "nadie", "WA corrida SRI OK", "string"],
    ["pipe_facturas_sri_wa_fallo_roles", "ADMIN", "Fallo SRI", "csv"],
    ["pipe_carga_inventario_mp_activo", "true", "Paso 4", "bool"],
    [
        "pipe_carga_inventario_mp_nota",
        "Idempotente vía sri_comprobantes_recibidos; no Drive",
        "Nota paso 4",
        "string",
    ],
    ["pipe_carga_inventario_mp_wa", "nadie", "WA si OK", "string"],
    # --- F PAR semanal ---
    ["par_modo", "semanal", "semanal | diario", "string"],
    ["par_cron_dia", "domingo", "Día PAR", "string"],
    ["par_cron_hora", "20:00", "Hora PAR", "string"],
    ["par_script", "calcular_par_levels.py", "Script PAR", "string"],
    ["par_reemplaza_pipeline_diario", "true", "No PAR en mediodia legacy", "bool"],
    ["par_post_accion", "recalcular_stock_sheets", "Tras PAR", "string"],
    ["par_wa", "nadie", "WA PAR OK", "string"],
    ["par_wa_fallo_roles", "ADMIN", "Fallo PAR", "csv"],
    # --- G Digest 8:00 ---
    ["digest_matutino_activo", "true", "Digest operativo", "bool"],
    ["digest_matutino_hora", "8:00", "Hora digest", "string"],
    ["digest_matutino_modo", "un_mensaje_por_area", "un_mensaje_por_area | un_mensaje_global", "string"],
    ["pipe_costos_activo", "true", "Costos teóricos", "bool"],
    ["pipe_costos_hora", "8:00", "Hora costos", "string"],
    ["pipe_costos_script", "recalcular_todos_costos.py", "Script costos", "string"],
    ["pipe_costos_wa", "nadie", "WA costos OK", "string"],
    ["alert_delta_costos_activo", "true", "Delta precio MP", "bool"],
    ["alert_delta_costos_umbral", "0.05", "5% (ver umbral_alerta_precio)", "float"],
    ["alert_delta_costos_fuente", "hist_precios", "Fuente Supabase", "string"],
    [
        "alert_delta_costos_incluir",
        "proveedor,cod_mp,nombre_mp,pct_anterior,pct_nuevo",
        "Campos mensaje",
        "csv",
    ],
    ["alert_delta_costos_roles_cocina", "ADMIN,JEFE_COCINA,ADMIN_COMPRAS,OPS_ALERTAS", "Delta cocina", "csv"],
    ["alert_delta_costos_roles_barra", "ADMIN,JEFE_BARRA,ADMIN_COMPRAS,OPS_ALERTAS", "Delta barra", "csv"],
    ["alert_stock_negativo_activo", "true", "Stock negativo 8am", "bool"],
    ["alert_stock_negativo_roles_cocina", "ADMIN,JEFE_COCINA,OPS_ALERTAS", "Negativos cocina", "csv"],
    ["alert_stock_negativo_roles_barra", "ADMIN,JEFE_BARRA,OPS_ALERTAS", "Negativos barra", "csv"],
    ["alert_bajo_par_activo", "true", "Bajo PAR 8am", "bool"],
    ["alert_bajo_par_roles_cocina", "ADMIN,JEFE_COCINA,OPS_ALERTAS", "PAR cocina", "csv"],
    ["alert_bajo_par_roles_barra", "ADMIN,JEFE_BARRA,OPS_ALERTAS", "PAR barra", "csv"],
    ["alert_pedidos_activo", "true", "Pedidos proveedor", "bool"],
    ["alert_pedidos_hora", "8:00", "Hora pedidos", "string"],
    ["alert_pedidos_barra_activo", "true", "Pedidos barra ON", "bool"],
    ["alert_pedidos_cocina_activo", "false", "Pedidos cocina OFF hasta inventario", "bool"],
    ["alert_pedidos_roles_barra", "ADMIN,JEFE_BARRA,ADMIN_COMPRAS,OPS_ALERTAS", "Destinatarios barra", "csv"],
    ["alert_pedidos_roles_cocina", "ADMIN,JEFE_COCINA,ADMIN_COMPRAS,OPS_ALERTAS", "Destinatarios cocina (futuro)", "csv"],
    ["alert_pedidos_respetar_ventana_proveedor", "true", "ventana_pedido BD_PROV", "bool"],
    # --- H Alertas SRI ---
    ["alert_sri_items_pendientes_activo", "true", "Solo sin match/PARCIAL", "bool"],
    ["alert_sri_items_pendientes_roles", "ADMIN_COMPRAS", "Mary", "csv"],
    ["alert_sri_items_pendientes_copia_admin", "true", "Copia Felipe", "bool"],
    ["alert_sri_items_pendientes_roles_copia", "ADMIN", "Copia admin", "csv"],
    ["alert_sri_items_pendientes_condicion", "sin_match_o_parcial", "Condición", "string"],
    ["alert_sri_corrida_ok_wa", "nadie", "Nunca WA SRI OK", "string"],
    ["alert_facturas_pendientes_hoja", "BD_ITEMS_PENDIENTES", "Hoja revisión", "string"],
    # --- I Áreas ---
    ["area_cocina_bodegas", "BOD-001,BOD-005", "Filtro cocina", "csv"],
    ["area_barra_bodegas", "BOD-002,BOD-003", "Filtro barra", "csv"],
    ["area_cocina_proveedor_tipo", "Cocina", "BD_PROV.Tipo", "string"],
    ["area_barra_proveedor_tipo", "Barra", "BD_PROV.Tipo", "string"],
    ["area_cocina_inventario_gestionado", "false", "Cocina agente OFF", "bool"],
    ["alert_cocina_wa_activo", "false", "WA digest/alertas cocina (false=solo barra)", "bool"],
    ["area_barra_inventario_gestionado", "true", "Barra agente ON", "bool"],
    [
        "area_cocina_pipelines_activos",
        "ventas_reconciliar,descargo",
        "Pipelines cocina",
        "csv",
    ],
    [
        "area_barra_pipelines_activos",
        "ventas_reconciliar,descargo,facturas_sri,carga_inventario_mp",
        "Pipelines barra",
        "csv",
    ],
]


def _is_estrategia_key(clave: str) -> bool:
    clave = (clave or "").strip()
    return any(clave.startswith(p) for p in ESTRATEGIA_PREFIXES)


def _ensure_bd_config(sh: gspread.Spreadsheet, force: bool) -> tuple[int, int]:
    try:
        ws = sh.worksheet("BD_CONFIG")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="BD_CONFIG", rows=300, cols=4)
        ws.update("A1:D1", [HEADERS_CONFIG])

    values = ws.get_all_values()
    if not values:
        ws.update("A1:D1", [HEADERS_CONFIG])
        values = ws.get_all_values()

    headers = [h.strip().lower() for h in values[0]]
    if "clave" not in headers or "valor" not in headers:
        ws.clear()
        ws.update("A1:D1", [HEADERS_CONFIG])
        values = ws.get_all_values()

    existing: dict[str, int] = {}
    for i, row in enumerate(values[1:], start=2):
        if not row:
            continue
        clave = (row[0] or "").strip()
        if clave:
            existing[clave] = i

    added = 0
    updated = 0
    rows_to_append: list[list[str]] = []

    for row in BD_CONFIG_ROWS:
        clave = row[0]
        if clave in existing:
            if force and _is_estrategia_key(clave):
                ws.update(f"A{existing[clave]}:D{existing[clave]}", [row])
                updated += 1
            continue
        rows_to_append.append(row)
        added += 1

    if rows_to_append:
        ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")

    return added, updated


def _build_estrategia_matrix() -> list[list[str]]:
    """Una hoja con secciones editables (matrices humanas)."""
    roles_header = [
        "persona",
        "rol_principal",
        "roles_adicionales",
        "env_allowlist",
        "env_wa",
        "telefono_wa",
        "activo",
        "notas",
    ]
    roles_rows = [
        ["Felipe Jaramillo", "ADMIN", "", "ALLOWLIST_SOCIO", "ALERTA_WA_FELIPE", "", "SI", "Control total"],
        ["Mary", "ADMIN_COMPRAS", "", "ALLOWLIST_ADMIN_COMPRAS", "ALERTA_WA_MARY", "", "SI", "Compras + alerta ítems pendientes SRI"],
        ["Eduardo", "JEFE_BARRA", "", "ALLOWLIST_JEFE_BARRA", "ALERTA_WA_EDUARDO", "", "SI", "Jefe barra"],
        ["Jacky", "JEFE_COCINA", "", "ALLOWLIST_JEFE_COCINA", "ALERTA_WA_JACKY", "", "SI", "Jefe cocina"],
        ["Moisés", "SOCIO", "OPS_ALERTAS", "ALLOWLIST_SOCIO", "ALERTA_WA_MOISES", "", "SI", "Socio + alertas ops 8am"],
        ["Israel", "SOCIO", "OPS_ALERTAS", "ALLOWLIST_SOCIO", "ALERTA_WA_ISRAEL", "", "SI", "Socio + alertas ops 8am"],
        ["Socios (otros)", "SOCIO", "", "ALLOWLIST_SOCIO", "", "", "SI", "Agregar teléfonos en .env ALLOWLIST_SOCIO"],
        ["Staff barra", "STAFF_BARRA", "", "ALLOWLIST_STAFF_BARRA", "", "", "SI", "Sin costos; completar teléfonos"],
        ["Staff cocina", "STAFF_COCINA", "", "ALLOWLIST_STAFF_COCINA", "", "", "SI", "Sin costos; completar teléfonos"],
    ]

    perm_header = [
        "capacidad",
        "ADMIN",
        "SOCIO",
        "ADMIN_COMPRAS",
        "JEFE_BARRA",
        "JEFE_COCINA",
        "STAFF_BARRA",
        "STAFF_COCINA",
        "OPS_ALERTAS",
        "notas",
    ]
    perm_rows = [
        ["Ver costos / USD", "SI", "SI", "SI", "SI", "SI", "NO", "NO", "SI", "OPS_ALERTAS hereda SOCIO"],
        ["Consulta ventas", "SI", "SI", "SI", "SI", "SI", "SI", "SI", "SI", ""],
        ["Receta ingredientes sin USD", "SI", "SI", "SI", "SI", "SI", "SI", "SI", "SI", ""],
        ["Inventario / bajo PAR / negativos", "SI", "SI", "SI", "SI", "SI", "SI", "SI", "SI", ""],
        ["Iniciar conteo", "SI", "NO", "SI", "SI", "SI", "NO", "NO", "NO", ""],
        ["Aprobar conteo", "SI", "NO", "NO", "SI", "SI", "NO", "NO", "NO", ""],
        ["Traslados", "SI", "NO", "NO", "SI", "SI", "NO", "NO", "NO", ""],
        ["PRODUCIR SUB", "SI", "NO", "NO", "SI", "SI", "SI", "SI", "NO", "Staff limitado por bodega"],
        ["Escritura total", "SI", "NO", "NO", "NO", "NO", "NO", "NO", "NO", ""],
        ["Recibe digest 8am ops", "SI", "NO", "SI", "SI", "SI", "NO", "NO", "SI", "Stock/PAR/delta/pedidos"],
        ["Alerta ítems pendientes SRI", "SI", "NO", "SI", "NO", "NO", "NO", "NO", "NO", "Mary + copia Felipe"],
    ]

    pipe_header = [
        "paso",
        "codigo",
        "activo",
        "scripts",
        "wa_si_ok",
        "wa_si_fallo",
        "notas",
    ]
    pipe_rows = [
        ["1", "ventas_reconciliar", "SI", "ventas_smartmenu.py → reconciliar_ventas_dia.py", "nadie", "ADMIN", "Cada hora 7-00"],
        ["2", "descargo", "SI", "descargo_inventario.py", "nadie", "ADMIN", "MP + SUB"],
        ["3", "facturas_sri", "SI", "procesar_facturas_sri.py", "nadie", "ADMIN; Mary si pendientes", "Portal según pipe_facturas_sri_horas_descarga"],
        ["4", "carga_inventario_mp", "SI", "(incluido en SRI)", "nadie", "ADMIN", ""],
        ["—", "costos_teoricos", "SI", "recalcular_todos_costos.py", "nadie", "ADMIN", "Diario 8:00"],
        ["—", "par_level", "SI", "calcular_par_levels.py", "nadie", "ADMIN", "Domingo 20:00 (reemplaza diario)"],
    ]

    alert_header = [
        "alerta",
        "activo",
        "hora",
        "roles_destino",
        "area",
        "condicion",
        "notas",
    ]
    alert_rows = [
        ["Ítems pendientes SRI", "SI", "al procesar", "ADMIN_COMPRAS + copia ADMIN", "—", "sin_match_o_parcial", "Solo Mary (+ Felipe copia)"],
        ["Stock negativo", "SI", "8:00", "ADMIN,JEFE_*,OPS_ALERTAS", "cocina|barra", "stock<0", "Digest por área"],
        ["Bajo PAR", "SI", "8:00", "ADMIN,JEFE_*,OPS_ALERTAS", "cocina|barra", "stock<par", ""],
        ["Delta costos >5%", "SI", "8:00", "ADMIN,JEFE_*,ADMIN_COMPRAS,OPS_ALERTAS", "cocina|barra", "hist_precios", ""],
        ["Pedidos proveedor", "SI", "8:00", "ADMIN,JEFE_*,ADMIN_COMPRAS,OPS_ALERTAS", "barra", "ventana BD_PROV", "Cocina OFF por ahora"],
        ["Fallo pipeline horario", "SI", "inmediato", "ADMIN", "—", "error", ""],
        ["Fallo PAR semanal", "SI", "dom 20:00", "ADMIN", "—", "error", ""],
    ]

    sched_header = ["parametro", "valor", "notas"]
    sched_rows = [
        ["Modo", "horario_secuencial", "Reemplaza TatamiCuadrante_* y TatamiFacturasSRI_*"],
        ["Horario", "7:00 → 00:00 cada 60 min", ""],
        ["Orden", "ventas → descargo → SRI → carga MP", "Secuencial estricto"],
        ["Legacy cuadrante", "DESACTIVAR al migrar", "sched_legacy_cuadrante_activo=false"],
    ]

    out: list[list[str]] = []
    out.append(["BD_ESTRATEGIA — Matrices Tatami (completar telefono_wa y revisar SI/NO)"])
    out.append([])
    out.append(["=== 1. ASIGNACIÓN PERSONAS → ROLES ==="])
    out.append(roles_header)
    out.extend(roles_rows)
    out.append([])
    out.append(["=== 2. MATRIZ PERMISOS (SI/NO) ==="])
    out.append(perm_header)
    out.extend(perm_rows)
    out.append([])
    out.append(["=== 3. PIPELINES ==="])
    out.append(pipe_header)
    out.extend(pipe_rows)
    out.append([])
    out.append(["=== 4. ALERTAS ==="])
    out.append(alert_header)
    out.extend(alert_rows)
    out.append([])
    out.append(["=== 5. SCHEDULER (referencia) ==="])
    out.append(sched_header)
    out.extend(sched_rows)
    out.append([])
    out.append(["Claves máquina en pestaña BD_CONFIG (mismo spreadsheet)."])
    return out


def _ensure_bd_estrategia(sh: gspread.Spreadsheet, force: bool) -> str:
    title = "BD_ESTRATEGIA"
    matrix = _build_estrategia_matrix()
    nrows = len(matrix) + 5
    ncols = max(len(r) for r in matrix) if matrix else 10

    try:
        ws = sh.worksheet(title)
        if force:
            ws.clear()
            ws.resize(rows=max(nrows, 80), cols=max(ncols, 12))
            ws.update(range_name="A1", values=matrix, value_input_option="USER_ENTERED")
            return "reescrita"
        existing = ws.get_all_values()
        if len(existing) <= 1:
            ws.resize(rows=max(nrows, 80), cols=max(ncols, 12))
            ws.update(range_name="A1", values=matrix, value_input_option="USER_ENTERED")
            return "rellenada (estaba vacía)"
        return "sin cambios (ya tiene datos; usar --force)"
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=max(nrows, 80), cols=max(ncols, 12))
        ws.update(range_name="A1", values=matrix, value_input_option="USER_ENTERED")
        return "creada"


def main() -> None:
    ap = argparse.ArgumentParser(description="Semilla BD_CONFIG + BD_ESTRATEGIA")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Sobrescribe claves estrategia en BD_CONFIG y reescribe BD_ESTRATEGIA",
    )
    args = ap.parse_args()

    sheet_id = os.getenv("SPREADSHEET_ID")
    if not has_google_credentials() or not sheet_id:
        print("ERROR: credenciales Google (GOOGLE_CREDENTIALS_JSON o GOOGLE_CREDENTIALS_PATH) y SPREADSHEET_ID en .env", file=sys.stderr)
        raise SystemExit(1)

    creds = google_credentials(SCOPES)
    sh = gspread.authorize(creds).open_by_key(sheet_id)

    added, updated = _ensure_bd_config(sh, force=args.force)
    est = _ensure_bd_estrategia(sh, force=args.force)

    print(f"BD_CONFIG: +{added} filas nuevas, {updated} actualizadas (--force estrategia)")
    print(f"BD_ESTRATEGIA: {est}")
    print(f"Spreadsheet: https://docs.google.com/spreadsheets/d/{sheet_id}")


if __name__ == "__main__":
    main()

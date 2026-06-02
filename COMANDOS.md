# Referencia de comandos — Tatami Agente

Ejecutar desde la carpeta `tatami-agente` (con `venv` activado o `python` del venv).

## Tabla rápida: comando → función

| Comando / script | Función principal |
|------------------|---------------------|
| `pipeline_diario.py` | Orquesta: ventas → **reconciliar grid vs hist** → descargo → facturas → recalcular stock → PAR. Sin `--fecha`, ventas/reconcilio usan **ayer** calendario en `America/Guayaquil` (ej. miércoles 12:00 → martes). |
| `reconciliar_ventas_dia.py` | Compara totales Smart Menu (grid) vs `hist_ventas`; sale 1 si no cuadra (antes del descargo). |
| `ventas_smartmenu.py` | Descarga ventas desde Smart Menu y escribe líneas en Supabase `hist_ventas` (opción `--reemplazar`, auditoría, histórico). |
| `ventas_smartmenu_total.py` | Calcula total del día (u horario) sumando el grid Smart Menu, sin tocar Supabase. |
| `descargo_inventario.py` | Genera `mov_inventario` (salidas por receta) desde `hist_ventas` y actualiza stock/costo en Sheets. |
| `procesar_facturas_drive.py` | Lee XML en Drive, matchea `BD_ITEMS_PROV`, registra entradas de inventario y precios; opciones para hoja de pendientes y backfill. |
| `calcular_par_levels.py` | Calcula consumo diario y actualiza `par_level` / consumo en Sheets desde recetas + ventas. |
| `recalcular_stock_sheets.py` | Recalcula **stock** y **`costo_unitario_ref`** por (MP × bodega); costo con política canónica (`costo_mp_canonico.py`). |
| `sync_costos_mp_desde_items_prov.py` | Alinea `BD_MP_SISTEMA.costo_unitario_ref` desde mediana de `precio_ref` (ítems prov). Ver `REGLAS_COSTOS_MP.md`. |
| `auditar_costos_mp_prov_vs_maestro.py` | Audita maestro vs catálogo prov; corrige discrepancias >2% (excluye ratios extremos). CSV en `logs/`. |
| `costo_mp_canonico.py` | Fuente única de costo USD/gr para subrecetas, recetas y escritura en maestro MP. |
| `bodegas_config.py` | Catálogo BOD-001…005, matriz de traslados, reglas descargo/ingreso (importado por otros módulos). |
| `subrecetas_detalle.py` | Carga y utilidades `BD_SUBRECETAS` / detalle (MP vs subreceta hijo, orden de producción). |
| `auditar_subrecetas.py` | Valida maestro de subrecetas (285 líneas, anidadas, MPs). |
| `calcular_costo_subrecetas.py` | Costo teórico lote/uni en `BD_SUBRECETAS` (lee MPs vía `costo_mp_canonico`, no el valor inflado en hoja). |
| `auditar_costos_subrecetas.py` | Compara costos en hoja vs recálculo (detecta regresiones). |
| `diagnostico_subs_alto_costo.py` | Desglose por MP de subrecetas con costo sospechoso (007, 012, 020, …). |
| `calcular_costo_recetas.py` | Costo teórico por plato en `BD_RECETAS` desde detalle (MP + subrecetas). |
| `auditar_costos_recetas.py` | Platos inflados + MPs sospechosos en recetas (CSV). |
| `corregir_factor_kg_gr.py` | Repara granel con precio/kg mal cargado como USD/gr (`factor` 1000 + mov ENTRADA). |
| `corregir_costo_mp_120.py` | Corrige MP 120 (papa) a costo USD/gr de referencia en mov + BD_MP_SISTEMA. |
| `corregir_costo_mp_263_fanta.py` | Corrige MP 263 (FANTA): precio caja ÷24 uni en mov + maestro + BD_RECETAS. |
| `actualizar_costos_masivo.py` | MPs manuales, bebidas pack×24, vinos ml, quesos; recalc stock + subrecetas + platos. |
| `setup_bd_subrecetas.py` | Crea pestañas y encabezados de subrecetas en Sheets (instalación). |
| `setup_staging_recetas_v2.py` | Formulario staging recetas plato (MP + SUB). |
| `setup_staging_subrecetas.py` | Formulario staging subrecetas (cab + detalle). |
| `promover_staging_recetas.py` | APROBADO → `BD_RECETAS_DETALLE`. |
| `promover_staging_subrecetas.py` | APROBADO → `BD_SUBRECETAS*`. |
| `reporte_semanal.py` | Genera reporte semanal (ventas, costos, precios, stock, texto para revisión). |
| `generar_pedidos.py` | Arma sugerencias de pedidos a proveedores según stock vs PAR y ventanas de compra. |
| `agente_tatami.py` | Launcher que invoca los scripts anteriores según subcomando (`ventas`, `descargo`, `facturas`, etc.). |
| `ejecutar_ventas_mediodia.ps1` | Solo ventas con **ayer** Ecuador (misma regla que `pipeline_diario` sin `--fecha`). Opcional si ya corres el pipeline completo a las 12:00. |
| `test_conexiones.py` | Comprueba conectividad con Supabase y Google Sheets. |
| `crear_bd_config.py` | Crea/rellena datos semilla en la hoja `BD_CONFIG`. |
| `backfill_hist_ventas_docs.py` | Rellena la tabla Supabase `hist_ventas_docs` agrupando desde `hist_ventas`. |
| `consolidar_facturas_xml_local.py` | Une/consolid XML de facturas desde una carpeta local (análisis fuera de Drive). |
| `limpiar_mov_duplicados.py` | Detecta y opcionalmente elimina movimientos duplicados en Supabase. |
| `asignar_cod_mov.py` | Herramienta para normalizar o asignar códigos en movimientos / Supabase. |
| `conteo_fisico.py` | Inventario físico cíclico: ciclo, snapshot desde Sheets, envío (JSON), aprobación, contabilización en `mov_inventario`. |
| `plantilla_conteo_sheets.py` | Crea la pestaña **CONTEO** (layout fijo) y opcionalmente la rellena desde `conteo_linea` tras el snapshot. |

---

## Pipeline diario

```bash
python pipeline_diario.py
python pipeline_diario.py --fecha 2026-05-11
python pipeline_diario.py --skip-ventas
python pipeline_diario.py --strict-ventas
python pipeline_diario.py --skip-reconciliar
```

Sin `--fecha`, **fecha objetivo** = día calendario **anterior** en `America/Guayaquil` (típico: tarea diaria a las **12:00** del día D carga ventas del día **D−1** completo, 00:00–23:59).

**Orden interno del pipeline:** ventas → reconciliar grid vs hist → descargo → facturas → recalcular stock → PAR  
(equivale a: `ventas_smartmenu.py` → `reconciliar_ventas_dia.py` → `descargo_inventario.py` → `procesar_facturas_drive` → `recalcular_stock_sheets.py` → `calcular_par_levels.py`).

| Flag | Efecto |
|------|--------|
| `--skip-ventas` | Omite el paso de ventas Smart Menu |
| `--skip-reconciliar` | Omite la reconciliación grid vs Supabase (**no recomendado**; el descargo puede correr sin cuadre) |
| `--strict-ventas` | Falla el pipeline si `ventas_smartmenu.py --strict` falla y envía alerta si está configurada |

Tras ventas, **`reconciliar_ventas_dia.py`** compara subtotal del grid con sumas en `hist_ventas` y el número de documentos; si no cuadra dentro de **`RECONCILIAR_TOL_ABS`** (default **0,05** USD), el pipeline **termina con error** y **`alertas_tatami`** puede enviar webhook / escribir log:

| Variable | Uso |
|----------|-----|
| `RECONCILIAR_TOL_ABS` | Tolerancia absoluta en USD para diferencia de subtotal (default `0.05`) |
| `TATAMI_ALERT_WEBHOOK_URL` | POST JSON si falla reconciliación o ventas en modo `--strict-ventas` |
| `TATAMI_ALERT_LOG_PATH` | Archivo local donde se añaden alertas (UTF-8) |
| `WHATSAPP_PHONE_NUMBER_ID` / `WHATSAPP_ACCESS_TOKEN` | Misma pareja que Meta para enviar alertas por WhatsApp |
| `ALERTA_WA_FELIPE` / `ALERTA_WA_MOISES` | Solo dígitos con código país (ej. `5939…`). Felipe: OK diario + detalle en fallos; Moisés: solo fallos relevantes |
| `ALERTA_WA_JACKY` / `ALERTA_WA_EDUARDO` | Confirmación bodega de ingreso en facturas (001/005 vs 002/003) |
| `TATAMI_WA_SKIP=1` | Desactiva envío WA (sigue log/webhook); útil para pruebas |
| `TATAMI_ALERT_STOCK_NEGATIVO=1` | Reactiva alertas WA/webhook por stock negativo tras **descargo** (por defecto **suspendidas**) |

Prueba manual de reconciliación:

```bash
python reconciliar_ventas_dia.py --fecha 2026-05-09
```

---

## Ventas Smart Menu → `hist_ventas`

```bash
python ventas_smartmenu.py --fecha 2026-05-05
python ventas_smartmenu.py --fecha 2026-05-05 --reemplazar
python ventas_smartmenu.py --strict
python ventas_smartmenu.py --fecha 2026-05-05 --strict
python ventas_smartmenu.py --audit 2026-05-05
python ventas_smartmenu.py --historico 2026-01-01 2026-01-31
```

| Flag | Efecto |
|------|--------|
| `--fecha` | Día `YYYY-MM-DD` (default: hoy) |
| `--reemplazar` | Borra `hist_ventas` de ese día antes de cargar |
| `--strict` | Exit code 1 si errores de insert o BD vacía con docs en grid |
| `--audit FECHA` | Solo auditoría Supabase (sin Smart Menu) |
| `--historico FECHA_INI FECHA_FIN` | Rango de días |

Modo interactivo (sin flags `--fecha` / `--reemplazar` / etc.): pregunta fecha por consola.

---

## Total rápido desde grid Smart Menu (sin Supabase)

```bash
python ventas_smartmenu_total.py --fecha 2026-05-05
python ventas_smartmenu_total.py --fecha 2026-05-05 --desde 12:00 --hasta 18:00
python ventas_smartmenu_total.py --modo con_iva
python ventas_smartmenu_total.py --fecha 2026-05-05 --incluir-anulados
```

| Flag | Efecto |
|------|--------|
| `--desde` / `--hasta` | Filtro horario `HH:MM` |
| `--modo sin_iva` \| `con_iva` | Subtotal sin IVA (default) o total con IVA |
| `--incluir-anulados` | Incluye documentos anulados en la suma |

---

## Descargo inventario (`hist_ventas` → movimientos)

```bash
python descargo_inventario.py
python descargo_inventario.py --fecha 2026-05-05
python descargo_inventario.py --fecha 2026-05-05 --rehacer
```

| Flag | Efecto |
|------|--------|
| `--fecha` | Solo ese día (opcional; sin flag = pendientes globales) |
| `--rehacer` | Con `--fecha`: resetea descargo del día y vuelve a procesar |

**Descargo por subreceta (líneas SUB en `BD_RECETAS_DETALLE`):** por defecto solo MP (`DESCARGO_SUBRECETAS=0`). Para activar semis:

```bash
# .env
DESCARGO_SUBRECETAS=1

# 1) Pseudo-MP SUB-xxx en BD_MP_SISTEMA (BOD-001 y BOD-002)
python sync_stock_subrecetas_maestro.py --dry-run
python sync_stock_subrecetas_maestro.py --produccion

# 2) Costos de subrecetas al día
python calcular_costo_subrecetas.py --produccion

# 3) Descargo ventas (resumen: Salidas MP / Salidas SUB)
python descargo_inventario.py --fecha 2026-05-05
```

Diseño completo: `PLAN_DESCARGO_SUBRECETAS.md`. Tests: `python -m unittest test_descargo_subreceta.py`.

---

## Facturas XML (Google Drive)

Requiere `GOOGLE_DRIVE_FACTURAS_FOLDER_ID` = ID de la carpeta (de la URL `.../folders/ID`). La cuenta de servicio del JSON debe tener acceso a esa carpeta. Se listan todos los `*.xml` no eliminados, incluidos los que Drive marca como `application/octet-stream` (antes solo se veían `text/xml` y `application/xml`).

```bash
python inspeccionar_xml_drive.py
python procesar_facturas_drive.py
python procesar_facturas_drive.py --dry-run
python procesar_facturas_drive.py --reprocesar
python procesar_facturas_drive.py --crear-hoja-items-pendientes
python procesar_facturas_drive.py --backfill-items-pendientes
python procesar_facturas_drive.py --backfill-items-pendientes --dry-run
```

| Flag | Efecto |
|------|--------|
| `--dry-run` | No escribe Supabase/Sheets |
| `--reprocesar` | Ignora facturas ya `COMPLETA` (riesgo de duplicar movimientos) |
| `--crear-hoja-items-pendientes` | Crea pestaña `BD_ITEMS_PENDIENTES` si no existe |
| `--reparar-hoja-items-pendientes` | Cabecera + columnas `estab`, `formato_compra`, `proveedor_logico`; backfill filas existentes |
| `--reparar-hoja-items-pendientes --solo-cabecera` | Solo agrega columnas faltantes (sin backfill) |
| `--backfill-estab-pendientes` | Rellena estab / formato / proveedor lógico en filas ya existentes |
| `--backfill-items-pendientes` | Recorre XML en Drive y registra ítems sin match en la hoja |
| `--backfill-items-pendientes --dry-run` | Simula backfill sin escribir |

---

## PAR / consumo (`calcular_par_levels`)

```bash
python calcular_par_levels.py
python calcular_par_levels.py --dry-run
```

---

## Recalcular stock en Sheets desde movimientos

```bash
python recalcular_stock_sheets.py
python recalcular_stock_sheets.py --produccion
python recalcular_stock_sheets.py --produccion --solo-costo
python recalcular_stock_sheets.py --produccion --cod-mp 064 --cod-bodega BOD-001
```

Sin `--produccion` = dry run (no escribe). Stock por **`(cod_mp_sistema, cod_bodega)`**; tipos `TRASLADO_*` incluidos.

**`costo_unitario_ref` (política canónica):** ver `costo_mp_canonico.py` y **`REGLAS_COSTOS_MP.md`** (factor de pack, gaseosas, MPs duplicados).

1. Mediana robusta de `precio_ref` en ítems prov activos (`precio_ref` = USD/unidad_base al cargar factura).
2. Fallback: promedio ENTRADAs en ventana **`COSTO_REF_DIAS_VENTANA`** (default **90**).
3. Recetas **no** dividen por factor; solo `cantidad × costo_unitario_ref`.

Tras subir facturas o corregir catálogo:

```bash
python sync_costos_mp_desde_items_prov.py --produccion
python calcular_costo_subrecetas.py --produccion
python auditar_costos_subrecetas.py --cod 007 012 020
```

**Regresión típica:** entradas viejas en `mov_inventario` con `costo_unitario` = precio de saco/caja en USD/gr (col 0,86, almidón 0,236) → al recalcular stock reaparecían lotes de subreceta inflados. La política anterior evita reescribir el maestro con esos valores.

**`factor` y `precio_ref` en `BD_ITEMS_PROV`:** van **por ítem proveedor** (`cod_item_prov`). El costo en subrecetas/platos usa la política canónica, no el promedio de factores del catálogo.

---

## Inventario multi-bodega

| Código | Nombre | Uso |
|--------|--------|-----|
| `BOD-001` | Cocina | Descargo ventas, conteo, traslados |
| `BOD-002` | Barra | Descargo ventas, conteo, traslados |
| `BOD-003` | Consignación (virtual) | Traslados ↔ barra; **no** descargo ventas |
| `BOD-004` | Limpieza | **Inactiva** (ignorar por ahora) |
| `BOD-005` | Bodega externa | Ingresos, traslados ↔ cocina/barra |

**Maestro:** una fila en `BD_MP_SISTEMA` por par **(cod_mp, cod_bodega)**. **PAR** y `consumo_diario_calculado` son **globales por cod_mp** (mismo valor en todas las filas del MP).

**Compras:** `BD_ITEMS_PROV.cod_bodega_destino` (obligatorio por ítem). Si al procesar la factura la bodega difiere del default (`bodegas_linea` sin `bodegas_confirmadas`), la línea queda **pendiente** y se alerta a Jacky (001/005) o Eduardo (002/003) vía `ALERTA_WA_JACKY` / `ALERTA_WA_EDUARDO`.

**Recetas (`BD_RECETAS_DETALLE`):** columnas  
`nombre_receta | cod_receta | variedad_smart_menu | nombre_subreceta | cod_subreceta | nombre_mp | cod_mp_sistema | cantidad | unidad_base | cod_bodega | merma_pct | es_opcional | pct_aplicacion`.  
Línea MP: `cod_mp` lleno. Línea subreceta: `cod_subreceta` + gramos por plato. Migrar layout: `python migrate_bd_recetas_detalle.py`.  
**Guía quitar MPs / agregar SUB en platos:** `ESQUEMA_RECETAS_SUBRECETAS.md` (matriz de trabajo + staging v2).

**Subrecetas (`BD_SUBRECETAS`):**  
`nombre_subreceta | cod_subreceta | rendimiento_estandar | unidad | activa | notas | costo_lote_estandar | costo_unitario_estandar | costo_calc_at`  
Recalcular costos (cadena completa): `python recalcular_todos_costos.py --produccion`  
(o paso a paso: `sync_costos_mp_desde_items_prov.py` → `calcular_costo_subrecetas.py` → `calcular_costo_recetas.py`)  
Diagnóstico: `python diagnostico_costos_pipeline.py` | `python auditar_precio_ref_vs_mp.py`  
Pipeline diario con carta: `python pipeline_diario.py --with-costos` (recomendado **1×/día**, no cada hora).  
**Decimales / locale:** `numeros_sheets.parse_numero_sheets` (coma decimal, miles con punto). **Costo ref por MP:** el mismo `costo_unitario_ref` en **todas las bodegas** (prioridad lectura `BOD-001`; ENTRADAs se promedian por MP sin separar bodega). La `cod_bodega` en recetas solo afecta inventario.  
Auditoría costos carta: `python auditar_costos_recetas.py` (CSV platos inflados + líneas MP sospechosas).  
Auditoría `precio_ref` sin dividir: `python auditar_precio_ref_unidad_base.py`.  
WhatsApp: tools `costo_plato`, `receta_ingredientes` (plato: cantidades + costos), `costo_subreceta` (semi: lote + desglose) y `auditar_costos_recetas`.  
**Detalle (`BD_SUBRECETAS_DETALLE`):**  
`nombre_subreceta | cod_subreceta_padre | nombre_subreceta_hijo | cod_subreceta_hijo | nombre_mp | cod_mp_sistema | cantidad | unidad_base | cod_bodega | merma_pct`  
Por fila: subreceta **hijo** (`cod_subreceta_hijo`) o **MP** (`cod_mp_sistema`). Cantidades por `rendimiento_estandar` del padre.  
Crear pestañas vacías: `python setup_bd_subrecetas.py` (no sobreescribe datos). Auditoría: `python auditar_subrecetas.py`. Módulo: `subrecetas_detalle.py`.  
**Producción / traslados:** ver §11 en `ENTIDADES_Y_FLUJOS.md`. Producción habitual en **`BOD-005`**; producir hijos antes que padres (`021` antes `022`, `036` antes `037`, etc.).

**Traslados WhatsApp:** tool `trasladar_mp` (matriz en `bodegas_config.py`) + recálculo automático.

SQL opcional: `sql/add_facturas_procesadas_meta_bodega.sql` (columna `meta` en `facturas_procesadas`).

---

## Staging formularios (recetas y subrecetas)

Libro staging: `STAGING_SPREADSHEET_ID` en `.env` (si no, usa el ID por defecto en `staging_common.py`).

**Recetas de plato (v2 — MP + SUB):**

```bash
python setup_staging_recetas_v2.py
python setup_staging_recetas_v2.py --hoja STAGING_RECETAS_V2
python promover_staging_recetas.py --dry-run
python promover_staging_recetas.py --produccion
```

Flujo Mary: llenar filas → `tipo_linea` MP o SUB → `estado` APROBADO → promover (marca PROMOVIDO). Tras promover: `calcular_costo_recetas.py --produccion`.

**Subrecetas (cabecera + detalle):**

```bash
python setup_staging_subrecetas.py
python promover_staging_subrecetas.py --dry-run
python promover_staging_subrecetas.py --produccion
python promover_staging_subrecetas.py --cab --produccion
python promover_staging_subrecetas.py --detalle --produccion
```

Promover **cabecera antes que detalle** (el detalle exige que `cod_subreceta_padre` exista en `BD_SUBRECETAS`).

**Sync stock semi (pseudo-MP):** `python sync_stock_subrecetas_maestro.py --produccion` — ver sección Descargo inventario y `PLAN_DESCARGO_SUBRECETAS.md`.

**Apps Script (menú en el libro staging):** pegar `scripts_apps_script/tatami_staging.gs` en Extensiones → Apps Script del [Masters Sheets](https://docs.google.com/spreadsheets/d/1TJu70BNG4i3it4y51Eg3YlDNswLkh1QGRt6v-qAyexU). Menú **Tatami Admin** → promover subrecetas (cab / det / ambos) y recetas v2.

---

## Reporte semanal

```bash
python reporte_semanal.py
python reporte_semanal.py --dry-run
```

---

## Pedidos (generación)

```bash
python generar_pedidos.py
python generar_pedidos.py --dry-run
```

---

## Launcher `agente_tatami`

```bash
python agente_tatami.py ventas
python agente_tatami.py ventas --historico 2026-01-01 2026-01-31
python agente_tatami.py descargo
python agente_tatami.py facturas
python agente_tatami.py facturas --dry-run
python agente_tatami.py par-levels
python agente_tatami.py par-levels --dry-run
python agente_tatami.py pedidos --dry-run
python agente_tatami.py pedidos
python agente_tatami.py reporte
python agente_tatami.py reporte --dry-run
```

---

## PowerShell (programador de tareas)

### Probar a mano (misma ruta que usará la tarea)

```powershell
Set-Location "C:\Users\Usuario\Desktop\Agente Tatami\tatami-agente"
.\ejecutar_pipeline_diario.ps1
# o solo ventas del día objetivo (ayer Ecuador):
.\ejecutar_ventas_mediodia.ps1
```

Requisitos: carpeta `tatami-agente` con `.env` válido, `venv\Scripts\python.exe` existente, y red a Smart Menu / Supabase según el paso.

### Recrear la tarea en Windows (Programador de tareas)

1. Ajusta `$Raiz` a la ruta real del repo en esta máquina.
2. PowerShell **como administrador** (o usuario que vaya a ejecutar la tarea con permisos suficientes).
3. El pipeline escribe log en `tatami-agente\logs\pipeline_diario_YYYYMMDD.log`.

**Opción A — Pipeline completo** (ventas + reconcilio + descargo + facturas + stock + PAR), recomendado si quieres que todo avance en cadena:

```powershell
$Raiz = "C:\Users\Usuario\Desktop\Agente Tatami\tatami-agente"
$Script = Join-Path $Raiz "ejecutar_pipeline_diario.ps1"
$accion = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""
schtasks /Delete /TN "TatamiPipelineDiario" /F 2>$null
schtasks /Create /TN "TatamiPipelineDiario" /TR "powershell.exe $accion" /SC DAILY /ST 12:00 /RL HIGHEST /F
schtasks /Query /TN "TatamiPipelineDiario" /V /FO LIST
```

**Opción B — Solo ventas** (útil si el pipeline largo lo corres manual y solo quieres automatizar la carga a `hist_ventas`). No combines A y B a la misma hora si ambos cargan el mismo día (duplicado innecesario).

```powershell
$Raiz = "C:\Users\Usuario\Desktop\Agente Tatami\tatami-agente"
$Script = Join-Path $Raiz "ejecutar_ventas_mediodia.ps1"
$accion = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""
schtasks /Delete /TN "TatamiVentasMediodia" /F 2>$null
schtasks /Create /TN "TatamiVentasMediodia" /TR "powershell.exe $accion" /SC DAILY /ST 12:00 /RL HIGHEST /F
```

**Probar sin esperar al calendario:** `schtasks /Run /TN "TatamiPipelineDiario"` (o el nombre que hayas usado).

**Si la tarea “corre” pero no actualiza ventas:** abre el `.log` del día en `logs\`; suele ser reconciliación que falla tras un reintento, credenciales Smart Menu, o `ventas_smartmenu` con error de red. Cambiar `/ST 12:05` si Smart Menu publica el grid unos minutos después de medianoche Ecuador.

**Tarea con “Ejecutar aunque el usuario no haya iniciado sesión”:** créala desde el GUI del Programador de tareas (General → opción de seguridad + contraseña) o con `schtasks /Create ... /RU DOMINIO\Usuario /RP contraseña` para que no dependa de una sesión abierta.

---

## Apps Script — libro maestro (unificado)

**Archivo a pegar:** `scripts_apps_script/tatami_maestro_unificado.gs`  
Incluye menús **Tatami** (promover pendientes) y **Conteo** (enviar inventario físico) en un solo `onOpen()`.

### Instalación

1. Google Sheets (maestro) → **Extensiones → Apps Script**.
2. Eliminar archivos `.gs` antiguos del proyecto (evitar `onOpen` duplicados).
3. **+** → Archivo de secuencia de comandos → nombre `tatami_maestro_unificado.gs`.
4. Copiar y pegar **todo** el contenido del archivo del repo.
5. **Guardar** → cerrar editor → **recargar** el libro (F5).
6. Verificar menús **Tatami** y **Conteo** en la barra.

### Botones (dibujos opcionales)

| Acción | Función a asignar |
|--------|-------------------|
| Promover materiales | `promoverPendientesAItemsProv` |
| Simular antes de promover | `promoverPendientesAItemsProvSimular` |
| Enviar conteo (solo hoja CONTEO) | `enviarConteoATatami` |

Clic en el dibujo → ⋮ → **Asignar secuencia de comandos**.

### Promover pendientes (`BD_ITEMS_PENDIENTES`)

Requiere por fila: `estado=PENDIENTE`, `cod_mp_asignado`, `cod_proveedor`, `cod_item_xml`.

Equivalente terminal:

```bash
python promover_pendientes_a_items_prov.py --dry-run
python promover_pendientes_a_items_prov.py
```

Si el mismo `cod_item_prov` quedó dos veces (descripciones distintas), unificar antes de seguir cargando facturas:

```bash
python unificar_items_prov_duplicados.py --dry-run
python unificar_items_prov_duplicados.py --produccion
```

### Conteo — propiedades del script

En Apps Script → **⚙️ Ajustes del proyecto → Propiedades del script**:

| Propiedad | Valor |
|-----------|--------|
| `TATAMI_CONTEO_API_URL` | `https://tu-host/api/conteo/enviar` |
| `TATAMI_CONTEO_SECRET` | Igual que `CONTEO_SHEETS_INGEST_SECRET` en `.env` del servidor |

Menú **Conteo → Enviar a Tatami** solo en la pestaña plantilla (`A6` = `line_no`, `B2` = `ciclo_id`).

### Iniciar conteo por WhatsApp (sin terminal)

El agente tiene tools `conteo_iniciar`, `conteo_listar_ciclos` y `conteo_ciclos_abiertos`. Equivale a:

`crear-ciclo` → `snapshot` → `plantilla_conteo_sheets --desde-ciclo-id`

Ejemplos al agente:

- «Inicia conteo de cocina» → `conteo_iniciar` con `cod_bodega=BOD-001`
- «Inicia conteo barra semana 21» → `cod_bodega=BOD-002`, `semana_iso=21`
- «Qué conteos están abiertos» → `conteo_ciclos_abiertos`

Comando directo en el chat (sin pasar por el modelo):

```text
INICIAR CONTEO BOD-001
INICIAR CONTEO BOD-002
```

Módulo Python reutilizable: `conteo_operaciones.iniciar_conteo_wa(...)`.

---

## Otros scripts útiles

| Comando | Uso |
|---------|-----|
| `python test_conexiones.py` | Prueba Supabase / Sheets |
| `python crear_bd_config.py` | Semilla hoja BD_CONFIG |
| `python backfill_hist_ventas_docs.py` | Backfill tabla `hist_ventas_docs` |
| `python consolidar_facturas_xml_local.py --dir RUTA ...` | Consolidar XML locales |
| `python limpiar_mov_duplicados.py` | Limpieza movimientos (`--produccion` para aplicar) |
| `python asignar_cod_mov.py` | Asignación códigos mov (`--commit`, `--solo-supabase`) |

---

## SQL Supabase (manual)

| Archivo | Uso |
|---------|-----|
| `sql/add_hist_ventas_estado_documento.sql` | Columnas `estado_documento` / `detalle_anulacion` en `hist_ventas` |
| `sql/inventario_fisico_conteo.sql` | Tablas `conteo_ciclo`, `conteo_linea`, `conteo_envio`, `conteo_envio_detalle` + triggers (inventario físico cíclico) |

---

## Inventario físico cíclico (conteo)

Modelo en Supabase (`sql/inventario_fisico_conteo.sql`). **Cíclico** = un **ciclo** por periodo (`anio` + `semana_iso`) y **bodega** (`cod_bodega`); opcional **`area_etiqueta`** (ej. BARRA, COCINA) para rotación por zona en la misma bodega. Cada ciclo tiene su `ciclo_id` (UUID en **B2** de la plantilla). Varios envíos al mismo ciclo van con `secuencia` 1, 2, … (correcciones). El **snapshot** hoy carga **todas** las MPs de esa bodega desde `BD_MP_SISTEMA`; filtrar snapshot solo a un **área** sería una mejora futura si en Sheets existe columna/criterio alineado a `area_etiqueta`.

CLI: **`conteo_fisico.py`** (`python agente_tatami.py conteo …`). **Plantilla Sheets:** `plantilla_conteo_sheets.py` + Apps Script **`scripts_apps_script/tatami_maestro_unificado.gs`**. Contrato: `ENTIDADES_Y_FLUJOS.md` §9.

### Enviar desde Sheets sin terminal (recomendado)

1. En el servidor (misma red que Smart Menu si aplica, o túnel HTTPS público), definir en `.env`:

| Variable | Uso |
|----------|-----|
| `CONTEO_SHEETS_INGEST_SECRET` | Secreto compartido; mismo valor en Apps Script `TATAMI_CONTEO_SECRET` |

2. Levantar la API (recomendado: mismo proceso que el webhook de WhatsApp, para un solo túnel HTTPS):

```bash
uvicorn whatsapp_webhook:app --host 0.0.0.0 --port 8000
```

El endpoint de ingestión es **`POST /api/conteo/enviar`** (header `X-Tatami-Conteo-Secret`). Alternativa legacy en puerto aparte: `uvicorn conteo_sheet_api:app --port 8765` (`POST /api/conteo/registrar-envio`).

3. En Apps Script del libro: **Propiedades del script** → `TATAMI_CONTEO_API_URL` = URL completa hasta el path, p. ej. `https://tu-host/api/conteo/enviar` → `TATAMI_CONTEO_SECRET` = mismo secreto que arriba.

4. En Sheets: menú **Conteo → Enviar a Tatami** (rellenar col. G y B2…B5). Respaldo manual: **Conteo → Exportar JSON (respaldo)**.

`plantilla_conteo_sheets.py` **sin** `--produccion` solo muestra plan (dry run). Con `--produccion` escribe la pestaña.

**Crear / rellenar plantilla (mismo `SPREADSHEET_ID` que el resto del agente):**

```bash
python plantilla_conteo_sheets.py
python plantilla_conteo_sheets.py --produccion
python plantilla_conteo_sheets.py --produccion --desde-ciclo-id <uuid-tras-snapshot>
python plantilla_conteo_sheets.py --produccion --sobreescribir --desde-ciclo-id <uuid>
```

Luego: **Extensiones > Apps Script**, pegar `scripts_apps_script/tatami_maestro_unificado.gs` (ver sección Apps Script arriba), guardar, recargar el libro, rellenar **conteo_fisico** (col. G). El `ciclo_id` en **B2** debe coincidir con Supabase. Usa el **`cod_bodega` real** de `BD_MP_SISTEMA` (ej. `BOD-001`, no un código inventado).

```bash
python conteo_fisico.py listar-ciclos
python conteo_fisico.py crear-ciclo --anio 2026 --semana-iso 19 --cod-bodega BOD-001 --produccion
python conteo_fisico.py snapshot --ciclo-id <uuid> --produccion
python conteo_fisico.py registrar-envio --ciclo-id <uuid> --archivo payload.json --produccion
python conteo_fisico.py aprobar --envio-id <uuid> --por "Moisés"
python conteo_fisico.py contabilizar --envio-id <uuid> --produccion --recalcular-sheets
```

Sin `--produccion`, `crear-ciclo`, `snapshot` y `registrar-envio` / `contabilizar` hacen **dry run**. `snapshot` con líneas ya cargadas requiere `--reemplazar`.

**Umbral “delta cero” en contabilización:** si `abs(delta) < conteo_delta_abs_tol` no se inserta `mov_inventario` (evita ajustes por redondeo). Default **0.001** unidades de la MP. Origen (en orden): flag `--tol`, variable `CONTEO_DELTA_ABS_TOL`, hoja **PARAMETROS** (clave `conteo_delta_abs_tol`), **BD_CONFIG** misma clave. La pestaña PARAMETROS usa las columnas `clave` / `valor` como BD_CONFIG.

Después de contabilizar (o si los movimientos se cargaron a mano), alinear stock en la hoja:

```bash
python recalcular_stock_sheets.py --produccion
```

---

*Última revisión según código en `tatami-agente`. Si un script cambia, actualizar esta lista.*

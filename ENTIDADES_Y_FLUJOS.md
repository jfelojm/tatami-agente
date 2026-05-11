# Entidades Tatami: Sheets vs Supabase, relaciones y gobierno de datos

Documento de referencia para alinear el código y la operación (actualizado tras auditoría del repo).

---

## 1. Inventario de entidades en Google Sheets

| Hoja | Rol | Quién escribe (automático) | Quién edita (humano) |
|------|-----|----------------------------|------------------------|
| **BD_MP_SISTEMA** | Maestro operativo de materia prima: stock, costo ref, PAR, consumo | Agente: `procesar_facturas_drive`, `descargo_inventario`, `recalcular_stock_sheets`, `calcular_par_levels`, `sync_stock_sheets_desde_mov`; lectura: chat, reportes, pedidos | Operación (ajustes puntuales si se permite) |
| **BD_ITEMS_PROV** | Catálogo ítem proveedor → MP: precios ref, unidad compra, factor | Agente: columnas `precio_ref`, `precio_unitario_xml`, `fecha_precio_ref` al procesar facturas | Compras / admin (altas, mapeos `cod_item_prov`) |
| **BD_PROV** | Proveedores (RUC, códigos) | Lectura para lookups | Admin |
| **BD_RECETAS_DETALLE** | Receta × variedad → ingredientes (MP, gramajes, merma) | Lectura para descargo y PAR | Producción / carta |
| **BD_PRODUCTOS** | Productos Smart Menu ↔ recetas | Lectura (`matching_productos`) | Admin |
| **BD_CONFIG** | Claves de configuración (`umbral_alerta_precio`, `par_level_dias_cobertura`, etc.) | `crear_bd_config.py` / manual | Admin |
| **MOV_INVENTARIO** (hoja) | **Legacy / manual**: captura movimientos en Sheets para subir a Supabase | `asignar_cod_mov.py` escribe `cod_mov` y sincroniza a BD | Operación (solo si usan flujo hoja, no el principal) |
| **FACTURAS_CONSOLIDADO_ITEMS** / hojas de consolidación | Ayuda matching facturas | `sugerir_matching_facturas`, `consolidar_facturas_xml_local` | Revisión compras |
| **Reportes / otras** | Consultas | Varios scripts | — |
| **Plantilla CONTEO** (por ciclo) | Captura del conteo físico en Google Sheets; vinculada a `conteo_ciclo.spreadsheet_id` / `sheet_name` (p. ej. pestaña `CONTEO`) | Apps Script “Iniciar” / “Enviar” (cuando existan) + backend | Bodega responsable (cocina, barra, consignación, bodega Israel, etc.) |

---

## 2. Inventario de tablas Supabase

| Tabla | Rol | Origen de escritura |
|-------|-----|---------------------|
| **mov_inventario** | Ledger canónico de movimientos (ENTRADA, SALIDA_VENTA, etc.) | `procesar_facturas_drive`, `descargo_inventario`, `whatsapp_webhook`, `asignar_cod_mov` (desde hoja legacy), ajustes |
| **hist_ventas** | Ventas línea a línea (Smart Menu / import) | `ventas_smartmenu.py`, pipelines; **descargo** marca `descargado` |
| **hist_ventas_docs** | Metadatos/documentación por venta (extensión opcional) | `backfill_hist_ventas_docs.py` |
| **hist_precios** | Auditoría de cambios de precio por variación vs referencia | `procesar_facturas_drive` (insert al superar umbral) |
| **facturas_procesadas** | Control de idempotencia por factura (XML Drive) | `procesar_facturas_drive` (upsert) |
| **conteo_ciclo** | Ciclo de inventario físico (semana ISO × bodega): planificación, estado, enlace al Sheet, `snapshot_at` | Backend / SQL al crear ciclo; Apps Script + API al iniciar conteo |
| **conteo_linea** | Líneas del ciclo (MP × bodega): snapshot de stock/costo al iniciar; `conteo_fisico` al capturar; deltas generados | Población al snapshot; sync desde Sheet o API |
| **conteo_envio** | Un registro por cada envío exitoso del Sheet (secuencia 1, 2… correcciones); estado de aprobación hacia contabilización | Backend al validar y persistir envío |
| **conteo_envio_detalle** | Copia inmutable por línea del envío; aprobación por ítem; `cod_mov_ajuste` enlaza con `mov_inventario` tras contabilizar | Insert junto con `conteo_envio` |

**No hay réplica en Sheets de:** `mov_inventario` completo, `hist_ventas` completo, `hist_precios` (correcto: volumen + integridad en BD).

---

## 3. Relaciones (resumen)

```
BD_PROV (RUC, cod_proveedor)
    └── BD_ITEMS_PROV (cod_item_prov, cod_mp_sistema, precio_ref, …)
              └── match ← XML factura (procesar_facturas_drive)

BD_MP_SISTEMA (cod_mp_sistema, stock_actual, costo_unitario_ref, par_level, consumo_diario_calculado)
    ↑ escritura stock/costo: movimientos + recálculo
    ↑ PAR/consumo: hist_ventas + BD_RECETAS_DETALLE + BD_CONFIG

hist_ventas ──descargo──► mov_inventario (SALIDA_VENTA) ──► (opcional) recalcular_stock_sheets
XML factura ──► mov_inventario (ENTRADA) + BD_ITEMS_PROV precios + hist_precios (si variación)

BD_RECETAS_DETALLE + hist_ventas ──► calcular_par_levels ──► BD_MP_SISTEMA (consumo_diario, par_level)
```

---

## 4. Brechas detectadas (estado anterior vs buenas prácticas)

| Tema | Estado / brecha | Acción |
|------|-----------------|--------|
| **Fuente de verdad movimientos** | Supabase `mov_inventario` es la ledger principal; existe hoja **MOV_INVENTARIO** para flujo legacy | Mantener un solo flujo “oficial” (recomendado: todo vía Supabase + scripts); la hoja solo si siguen cargando movimientos a mano |
| **HIST_PRECIOS columnas** | El código inserta: cod_hist, descripciones, códigos, factura, precios, variacion_pct, estado=PENDIENTE, observaciones. **No** rellena `fecha_decision` (humano / workflow posterior) | Correcto: decisión manual posterior en Supabase o UI |
| **Primera factura / precio_ref vacío** | Actualiza **Sheets** pero **no** inserta fila en `hist_precios` (solo hay insert si variación > umbral) | Aceptable; si negocio quiere histórico también en primera compra, ampliar regla |
| **calcular_par_levels** | Solo escribía PAR/consumo si `par_level > 0` → MPs sin ventas recientes quedaban con valores viejos | **Corregido:** ahora escribe consumo y PAR para todas las filas (incluye 0) |
| **batch_update Sheets** | Debe usar `value_input_option=USER_ENTERED` | Ya aplicado en módulos tocados |
| **Stock tras descargo vs recálculo** | `descargo_inventario` descuenta stock en memoria y Sheets; `recalcular_stock_sheets` recalcula desde suma de `mov_inventario` | Tras descargos masivos, conviene job de reconciliación (`recalcular_stock_sheets`) para evitar drift |
| **Orquestación** | Scripts sueltos sin pipeline único documentado | Definir orden diario (ver §6) en scheduler |

---

## 5. Quién hace qué

| Actor | Acciones |
|-------|----------|
| **Compras / admin** | Alta/edición **BD_ITEMS_PROV**, **BD_PROV**, resolver WARN de facturas sin match |
| **Operación / bodega** | Lee **BD_MP_SISTEMA**; movimientos manuales solo si proceso lo define (idealmente vía Supabase o formulario, no doble libro) |
| **Agente (facturas)** | Parse XML Drive → match → `mov_inventario` ENTRADA → actualiza **BD_ITEMS_PROV** precios + **BD_MP_SISTEMA** stock/costo por factura; `hist_precios` si variación; `facturas_procesadas` |
| **Agente (ventas)** | Carga `hist_ventas`; **descargo** genera SALIDA_VENTA y actualiza stock en Sheets para MPs tocados |
| **Agente (planeación)** | **calcular_par_levels**: `consumo_diario_calculado` y `par_level` desde ventas + recetas + **BD_CONFIG** |
| **Mantenimiento** | **recalcular_stock_sheets** alinear stock/costo con movimientos; **limpiar_mov_duplicados** si hubo reprocesos |
| **Bodega (conteo)** | Completa la plantilla Sheet del ciclo; “Enviar” solo con filas válidas (sin vacíos; **0** es cantidad válida) |
| **Moisés (conteo)** | Revisa envíos (`conteo_envio` / detalle); aprueba o rechaza por línea; tras contabilizar en `mov_inventario`, conviene **recalcular_stock_sheets --produccion** |

---

## 6. Orden sugerido de jobs (una “corrida” diaria o en cadena)

1. Ingesta ventas → `hist_ventas` (Smart Menu / import).
2. **descargo_inventario** (ventas pendientes) → `mov_inventario` + stock en **BD_MP_SISTEMA** (parcial).
3. **procesar_facturas_drive** (sin `--reprocesar` salvo excepción) → entradas + precios.
4. **recalcular_stock_sheets --produccion** (opcional diario; recomendable semanal mínimo) → `stock_actual` y `costo_unitario_ref` alineados con Supabase.
5. **calcular_par_levels** (sin `--dry-run`) → `consumo_diario_calculado` + `par_level`.

Frecuencia PAR/consumo: al menos **diaria** si las ventas se cargan cada día; si no, tras cada carga de `hist_ventas` procesable.

6. **Inventario físico cíclico (cuando aplique):** tras registrar ajustes en Supabase `mov_inventario` desde el flujo de aprobación del conteo, ejecutar **`recalcular_stock_sheets --produccion`** para alinear **BD_MP_SISTEMA** con el ledger (opción A acordada).

---

## 7. Gobierno futuro

- **Una fuente de verdad por entidad:** maestros operativos en Sheets donde el equipo edita; transacciones y auditoría en Supabase.
- **No duplicar edición** de la misma columna en dos sistemas sin sincronización definida.
- **Política de reprocesamiento:** evitar `--reprocesar` en facturas salvo corrección puntual + limpieza de duplicados.
- **Documentar** quién aprueba `hist_precios.estado` y dónde se setea `fecha_decision`.
- **Opcional:** vista Sheets o Data Studio desde Supabase para `hist_precios` y `mov_inventario` (solo lectura).

---

## 8. Archivos Python clave por entidad

| Necesidad | Script principal |
|-----------|------------------|
| Facturas XML Drive | `procesar_facturas_drive.py` |
| Stock desde movimientos | `recalcular_stock_sheets.py` |
| Descargo ventas | `descargo_inventario.py` |
| PAR y consumo | `calcular_par_levels.py` |
| Config | `config_sheets.py` (`cfg()` → BD_CONFIG) |
| Duplicados mov | `limpiar_mov_duplicados.py` |
| Hoja MOV legacy | `asignar_cod_mov.py` |
| Inventario físico cíclico (conteo) | DDL: `sql/inventario_fisico_conteo.sql`. CLI: `conteo_fisico.py` (ciclo, snapshot, envío JSON, aprobar, contabilizar → `mov_inventario`). Apps Script + endpoint HTTP y RLS: **pendiente** si se expone a cliente |

---

## 9. Inventario físico cíclico (conteo)

**Objetivo:** conteo por **bodega** (cocina, barra, consignación, bodega Israel, etc.) con captura en **Google Sheets**, snapshot de stock/costo al **iniciar** conteo, **registro de cada envío** con validación estricta (ninguna fila vacía en columnas obligatorias; **0** es válido), comparación y **aprobación** (Moisés), y **contabilización solo vía** `mov_inventario` seguida de **`recalcular_stock_sheets --produccion`**.

| Tabla | Idea clave |
|-------|------------|
| `conteo_ciclo` | Un ciclo por periodo (`anio`, `semana_iso`) y `cod_bodega`. Estados: `PLANIFICADO` → `SNAPSHOT_LISTO` → `BORRADOR_CONTEO` → `CONTABILIZADO` \| `ANULADO`. |
| `conteo_linea` | Una fila por `(ciclo_id, cod_mp_sistema, cod_bodega)`. Snapshots: `stock_sistema_snapshot`, `costo_unitario_ref_snapshot`. `conteo_fisico` NULL en borrador; obligatorio al enviar. Columnas generadas: `delta_calculado`, `valor_delta_estimado`. |
| `conteo_envio` | Cada “Enviar” exitoso = nueva fila; `secuencia` 1, 2… para correcciones. `estado_aprobacion`: `PENDIENTE_REVISION`, `APROBADO_TOTAL`, `APROBADO_PARCIAL`, `RECHAZADO`, `CONTABILIZADO`. |
| `conteo_envio_detalle` | Congelado por línea al enviar. `estado_linea` por ítem; `cod_mov_ajuste` apunta al movimiento en `mov_inventario` después de contabilizar. |

### Contrato HTTP (borrador): registrar envío desde Sheets / cliente

Ruta sugerida: `POST /api/conteo/ciclos/{ciclo_id}/envios` (o el prefijo que use el servicio; mismo cuerpo).

**Autenticación:** definir en implementación (p. ej. secreto en header `Authorization: Bearer …` o API key solo en backend/Apps Script). No exponer service role al navegador.

**Precondiciones del ciclo:** el servidor debe rechazar el envío si `conteo_ciclo.estado` no permite captura (recomendado: solo `BORRADOR_CONTEO`; opcionalmente `SNAPSHOT_LISTO` si el primer envío pasa el ciclo a `BORRADOR_CONTEO` en la misma transacción). Rechazar si el ciclo está `CONTABILIZADO` o `ANULADO`.

**Cuerpo JSON (mínimo):**

```json
{
  "spreadsheet_id": "1abc…",
  "sheet_name": "CONTEO",
  "enviado_por": "Nombre operador",
  "enviado_por_contacto": "+593… o correo",
  "observaciones": "opcional",
  "lines": [
    {
      "line_no": 2,
      "cod_mp_sistema": "MP-001",
      "cod_bodega": "BOD01",
      "conteo_fisico": 12.5,
      "notas": "opcional"
    }
  ]
}
```

**Reglas de validación estrictas (servidor):**

1. **`lines` no vacío** y debe cubrir **exactamente** el conjunto de filas activas del ciclo en `conteo_linea` (mismo `ciclo_id`): ni faltan MP, ni sobran claves. Emparejamiento recomendado por `(cod_mp_sistema, cod_bodega)`; `line_no` es auditabilidad opcional.
2. **`conteo_fisico`:** obligatorio en cada línea; debe ser **número** (JSON number). **`0` es válido.** No aceptar `null`, cadena vacía, ni celda “vacía” mapeada a ausencia de campo.
3. **`cod_mp_sistema` / `cod_bodega`:** obligatorios, no vacíos; deben coincidir con la línea del ciclo.
4. **Snapshot:** para cada línea, tomar de BD los valores congelados del envío: `stock_sistema_snapshot`, `costo_unitario_ref_snapshot`, `nombre_mp`, `unidad_base` desde `conteo_linea` (no desde el payload del cliente, salvo que en el futuro se defina reconciliación explícita). Calcular `delta_calculado = conteo_fisico - stock_sistema_snapshot` y `valor_delta_estimado` igual que en columna generada (o `null` si no hay costo snapshot).
5. **Secuencia:** `secuencia = COALESCE(MAX(secuencia), 0) + 1` por `ciclo_id` dentro de la misma transacción que inserta `conteo_envio` + filas en `conteo_envio_detalle`.
6. **`payload_hash`:** opcional; recomendado SHA-256 del cuerpo canónico (JSON ordenado o string del Sheet) para idempotencia/dedupe.
7. Tras insert exitoso: actualizar `conteo_linea.conteo_fisico` (y `notas` si vienen) para reflejar el último envío aceptado; opcional: dejar `conteo_ciclo.estado` en `BORRADOR_CONTEO` hasta aprobación.

**Idempotencia:** header opcional `Idempotency-Key: <uuid>`. Si se repite la misma clave y mismo `ciclo_id` dentro de una ventana (p. ej. 24 h), devolver el mismo `envio_id` sin duplicar filas.

**Respuesta 201 Created:**

```json
{
  "envio_id": "uuid",
  "ciclo_id": "uuid",
  "secuencia": 1,
  "lineas_persistidas": 42,
  "payload_hash": "sha256…",
  "estado_aprobacion": "PENDIENTE_REVISION"
}
```

**Errores (cuerpo JSON sugerido `{ "error": { "code": "…", "message": "…", "details": {} } }`):**

| HTTP | `code` | Cuándo |
|------|--------|--------|
| 400 | `VALIDATION_LINES_EMPTY` | `lines` ausente o arreglo vacío |
| 400 | `VALIDATION_MISSING_LINE` | Falta alguna fila de `conteo_linea` del ciclo |
| 400 | `VALIDATION_UNKNOWN_LINE` | Viene un `(cod_mp_sistema, cod_bodega)` que no pertenece al ciclo |
| 400 | `VALIDATION_CONTEO_REQUIRED` | `conteo_fisico` ausente, `null` o no numérico |
| 400 | `VALIDATION_DUPLICATE_KEY` | Duplicado de MP+bodega dentro del payload |
| 400 | `VALIDATION_SHEET_MISMATCH` | `spreadsheet_id` / `sheet_name` no coinciden con `conteo_ciclo` (si se exige verificación) |
| 401 | `UNAUTHORIZED` | Token o clave inválida |
| 404 | `CICLO_NOT_FOUND` | `ciclo_id` inexistente |
| 409 | `CICLO_WRONG_STATE` | Estado del ciclo no permite envío |
| 409 | `SNAPSHOT_NOT_READY` | Aún no hay filas en `conteo_linea` o snapshot incompleto |

**Pendiente operativo (si se desea flujo 100 % desde Sheet):** políticas **RLS** con anon key; endpoint HTTP que replique las validaciones de `conteo_fisico.py registrar-envio`; Apps Script en la plantilla. La contabilización en `mov_inventario` y **`recalcular_stock_sheets --produccion`** ya pueden ejecutarse vía CLI (`conteo_fisico.py contabilizar --recalcular-sheets`).

**Endpoint relacionado (borrador):** `POST /api/conteo/ciclos/{ciclo_id}/snapshot` o acción “Iniciar” que lea stock/costo desde la fuente oficial (Sheets/Supabase), inserte/actualice `conteo_linea`, ponga `snapshot_at` y `conteo_ciclo.estado = 'SNAPSHOT_LISTO'` o `'BORRADOR_CONTEO'`.

---

## 10. Ejecución automatizada (`pipeline_diario.py`)

Desde `tatami-agente` (idealmente con el `venv`):

```bash
python pipeline_diario.py
```

Omitir ingestión Smart Menu si no hay red local / sesión:

```bash
python pipeline_diario.py --skip-ventas
```

Orden interno: **ventas** (opcional) → **descargo_inventario** → **procesar_facturas_drive** → **recalcular_stock_sheets --produccion** → **calcular_par_levels**. Programar este comando en el Programador de tareas de Windows (o cron) con la frecuencia acordada.

### Ventas Smart Menu a mediodía (12:00)

El local opera hasta pasada la medianoche (ej. sábado ~1:00); cargar ventas a las **12:00 del día siguiente** permite ver en **hist_ventas** el cierre del ciclo nocturno como **ventas del día calendario** que Smart Menu reporta para esa fecha.

- Script dedicado: **`ejecutar_ventas_mediodia.ps1`** (usa la fecha **local** de Windows y `ventas_smartmenu.py --fecha`).
- **Desactivar o borrar** la tarea programada anterior si estaba a las **23:00** (11 PM).

**Crear tarea diaria a las 12:00** (PowerShell como administrador, ajustar la ruta si tu carpeta difiere):

```powershell
$accion = "-NoProfile -ExecutionPolicy Bypass -File `"C:\Users\Usuario\Desktop\Agente Tatami\tatami-agente\ejecutar_ventas_mediodia.ps1`""
schtasks /Create /TN "TatamiVentasMediodia" /TR "powershell.exe $accion" /SC DAILY /ST 12:00 /RL HIGHEST /F
```

Comprobar: `schtasks /Query /TN "TatamiVentasMediodia"`.

El **pipeline completo** (`pipeline_diario.py`) puede seguir en otro horario (ej. después del mediodía o noche) o ejecutarse manualmente; si quieres que **todo** el pipeline corra a las 12:00, programa esa tarea con `pipeline_diario.py` en lugar del script de solo ventas.

---

*Generado para actualización de proyecto y alineación con Claude / equipo.*

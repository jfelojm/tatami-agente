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

---

## 2. Inventario de tablas Supabase

| Tabla | Rol | Origen de escritura |
|-------|-----|---------------------|
| **mov_inventario** | Ledger canónico de movimientos (ENTRADA, SALIDA_VENTA, etc.) | `procesar_facturas_drive`, `descargo_inventario`, `whatsapp_webhook`, `asignar_cod_mov` (desde hoja legacy), ajustes |
| **hist_ventas** | Ventas línea a línea (Smart Menu / import) | `ventas_smartmenu.py`, pipelines; **descargo** marca `descargado` |
| **hist_ventas_docs** | Metadatos/documentación por venta (extensión opcional) | `backfill_hist_ventas_docs.py` |
| **hist_precios** | Auditoría de cambios de precio por variación vs referencia | `procesar_facturas_drive` (insert al superar umbral) |
| **facturas_procesadas** | Control de idempotencia por factura (XML Drive) | `procesar_facturas_drive` (upsert) |

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

---

## 6. Orden sugerido de jobs (una “corrida” diaria o en cadena)

1. Ingesta ventas → `hist_ventas` (Smart Menu / import).
2. **descargo_inventario** (ventas pendientes) → `mov_inventario` + stock en **BD_MP_SISTEMA** (parcial).
3. **procesar_facturas_drive** (sin `--reprocesar` salvo excepción) → entradas + precios.
4. **recalcular_stock_sheets --produccion** (opcional diario; recomendable semanal mínimo) → `stock_actual` y `costo_unitario_ref` alineados con Supabase.
5. **calcular_par_levels** (sin `--dry-run`) → `consumo_diario_calculado` + `par_level`.

Frecuencia PAR/consumo: al menos **diaria** si las ventas se cargan cada día; si no, tras cada carga de `hist_ventas` procesable.

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

---

## 9. Ejecución automatizada (`pipeline_diario.py`)

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

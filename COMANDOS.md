# Referencia de comandos — Tatami Agente

Ejecutar desde la carpeta `tatami-agente` (con `venv` activado o `python` del venv).

## Tabla rápida: comando → función

| Comando / script | Función principal |
|------------------|---------------------|
| `pipeline_diario.py` | Orquesta el día: ventas → descargo → facturas Drive → recalcular stock Sheets → PAR/consumo en `BD_MP_SISTEMA`. |
| `ventas_smartmenu.py` | Descarga ventas desde Smart Menu y escribe líneas en Supabase `hist_ventas` (opción `--reemplazar`, auditoría, histórico). |
| `ventas_smartmenu_total.py` | Calcula total del día (u horario) sumando el grid Smart Menu, sin tocar Supabase. |
| `descargo_inventario.py` | Genera `mov_inventario` (salidas por receta) desde `hist_ventas` y actualiza stock/costo en Sheets. |
| `procesar_facturas_drive.py` | Lee XML en Drive, matchea `BD_ITEMS_PROV`, registra entradas de inventario y precios; opciones para hoja de pendientes y backfill. |
| `calcular_par_levels.py` | Calcula consumo diario y actualiza `par_level` / consumo en Sheets desde recetas + ventas. |
| `recalcular_stock_sheets.py` | Recalcula números de stock en `BD_MP_SISTEMA` a partir de `mov_inventario`. |
| `reporte_semanal.py` | Genera reporte semanal (ventas, costos, precios, stock, texto para revisión). |
| `generar_pedidos.py` | Arma sugerencias de pedidos a proveedores según stock vs PAR y ventanas de compra. |
| `agente_tatami.py` | Launcher que invoca los scripts anteriores según subcomando (`ventas`, `descargo`, `facturas`, etc.). |
| `ejecutar_ventas_mediodia.ps1` | Ejecuta solo la carga de ventas del día (fecha local Windows); pensado para Programador de tareas. |
| `test_conexiones.py` | Comprueba conectividad con Supabase y Google Sheets. |
| `crear_bd_config.py` | Crea/rellena datos semilla en la hoja `BD_CONFIG`. |
| `backfill_hist_ventas_docs.py` | Rellena la tabla Supabase `hist_ventas_docs` agrupando desde `hist_ventas`. |
| `consolidar_facturas_xml_local.py` | Une/consolid XML de facturas desde una carpeta local (análisis fuera de Drive). |
| `limpiar_mov_duplicados.py` | Detecta y opcionalmente elimina movimientos duplicados en Supabase. |
| `asignar_cod_mov.py` | Herramienta para normalizar o asignar códigos en movimientos / Supabase. |
| `sync_stock_sheets_desde_mov.py` | Alinea stock en Sheets con lo derivado de movimientos. |

---

## Pipeline diario

```bash
python pipeline_diario.py
python pipeline_diario.py --skip-ventas
python pipeline_diario.py --strict-ventas
```

| Flag | Efecto |
|------|--------|
| `--skip-ventas` | Omite el paso de ventas Smart Menu |
| `--strict-ventas` | Falla el pipeline si `ventas_smartmenu.py --strict` falla |

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

---

## Facturas XML (Google Drive)

```bash
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
```

Sin `--produccion` = dry run (no escribe).

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

```powershell
.\ejecutar_ventas_mediodia.ps1
```

Ejecuta `ventas_smartmenu.py --fecha` con la fecha local de Windows (requiere `venv\Scripts\python.exe`).

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
| `python sync_stock_sheets_desde_mov.py` | Sincronía stock desde movimientos |

---

## SQL Supabase (manual)

| Archivo | Uso |
|---------|-----|
| `sql/add_hist_ventas_estado_documento.sql` | Columnas `estado_documento` / `detalle_anulacion` en `hist_ventas` |

---

*Última revisión según código en `tatami-agente`. Si un script cambia, actualizar esta lista.*

# Esquema: recetas de platos con subrecetas

Guía para **modificar `BD_RECETAS_DETALLE`**, el **formulario (staging)** y evitar doble descargo de inventario.

Relacionado: `BD_SUBRECETAS` / `BD_SUBRECETAS_DETALLE`, `ENTIDADES_Y_FLUJOS.md` §11, `recetas_detalle.py`.

---

## 1. Tres capas (no mezclar)

| Capa | Hoja | Qué define |
|------|------|------------|
| **Subreceta** | `BD_SUBRECETAS` + `_DETALLE` | Cómo se **produce** un semi (MPs + subrecetas hijas). Rendimiento = lote estándar. |
| **Plato (venta)** | `BD_RECETAS_DETALLE` | Qué se **consume por plato vendido** al cliente (MPs sueltos y/o semis). |
| **Producción** | `mov_inventario` (futuro) | Baja MPs al producir; entra stock del semi. |

**Regla:** lo que ya está dentro de una subreceta **no** va otra vez como MP en el plato.

---

## 2. `BD_RECETAS_DETALLE` — columnas (ya migradas)

| Col | Campo | Uso |
|-----|--------|-----|
| A | `nombre_receta` | Nombre legible del plato |
| B | `cod_receta` | Código carta (ej. `017`) |
| C | `variedad_smart_menu` | Variedad Smart Menu o vacío |
| D | `nombre_subreceta` | Descriptivo (opcional) |
| E | `cod_subreceta` | **Solo si la línea es semi** (ej. `003`, `037`) |
| F | `nombre_mp` | **Solo si la línea es MP** |
| G | `cod_mp_sistema` | **Solo si la línea es MP** |
| H | `cantidad` | Gramos/ml/uni **por 1 plato vendido** |
| I | `unidad_base` | `gr`, `ml`, `uni` (igual que maestro MP o `gr`/`uni` para semi) |
| J | `cod_bodega` | Bodega de **consumo** en venta: `BOD-001` cocina, `BOD-002` barra |
| K | `merma_pct` | Decimal (ej. `0` o `0.05`) |
| L | `es_opcional` | `SI` / `NO` |
| M | `pct_aplicacion` | `1` = 100% |

### Por fila: exactamente un tipo

| Tipo | `cod_subreceta` | `cod_mp_sistema` | `cantidad` |
|------|-----------------|------------------|------------|
| **MP** | vacío | lleno | gr/ml/uni por plato |
| **Subreceta** | lleno (ej. `050`) | vacío | gr o uni **por plato** |

No llenar ambos. No dejar ambos vacíos.

### Unidades en platos

| Semi en cabecera | En plato usar |
|------------------|---------------|
| `unidad` = **gr** | `cantidad` en **gr** por plato (ej. 30) |
| `unidad` = **uni** | `cantidad` en **uni** por plato (ej. 1 choko = 1 uni del semi 051 si existiera) |

---

## 3. Qué eliminar y qué agregar (lógica de migración)

### Paso A — Inventariar el plato actual

Por cada `cod_receta` (+ `variedad_smart_menu`):

1. Listar todas las líneas **MP** actuales.
2. Marcar cuáles ya están cubiertas por una **subreceta activa** en `BD_SUBRECETAS`.

### Paso B — Sustituir bloques por subreceta

Si el plato llevaba MPs que hoy son una subreceta:

| Acción | Ejemplo Tatami Burger `017` |
|--------|----------------------------|
| **Quitar** MPs sueltos | tocino, cebolla caramelizada, **pan bao (MP 50)** |
| **Agregar** línea SUB | `003` mermelada tocino — 30 **gr** |
| **Agregar** línea SUB | `005` cebollas caramelizadas — 20 **gr** |
| **Agregar** línea SUB | `006` pan bao — **2** **uni** por plato (como hoy con MP 50) |
| **Mantener** MPs que no son semi | carne `004`, queso, vegetales sueltos, etc. |

### Paso C — Subreceta anidada en plato

En el plato referencia el semi **más armado** que sirves:

| Correcto | Incorrecto |
|----------|------------|
| Plato usa `037` kimchi caramelizado (30 gr) | Plato usa `036` kimchi + azúcar sueltos |
| Plato usa `004` carne hamburguesa (150 gr) | Plato usa carne molida + chimi sueltos si `004` ya los incluye |
| Plato usa `006` pan bao (**uni** por plato, ej. 2) | Plato usa **MP 50** “PAN BAO” (comprado/armado como MP suelto) |
| Postre usa semi choko armado (subreceta padre) | Plato lista cake 050 + nutella + … si ya es una subreceta padre |

### Paso D — Checklist por plato

- [ ] Ningún MP duplicado respecto al detalle de una subreceta usada en el mismo plato  
- [ ] `cod_bodega` = cocina o barra (donde se arma/sirve), no `BOD-005` salvo que vendan desde externa  
- [ ] `cantidad` = por **1** unidad vendida en Smart Menu  
- [ ] Subreceta existe en cabecera y `activa` = SI  

### MPs frecuentes a quitar (reemplazo por SUB)

| Quitar en plato (MP) | `cod_mp` hoy | Poner en plato (SUB) | `cod_subreceta` | Unidad en plato |
|----------------------|--------------|----------------------|-----------------|-----------------|
| PAN BAO | **50** | pan bao | **006** | **uni** (ej. 2 por bao/hamburguesa) |
| TOCINO / mermelada | según ficha | mermelada de tocino | **003** | gr |
| Cebolla caramelizada | según ficha | cebollas caramelizadas | **005** | gr |
| Carne + chimi por separado | varios | carne de hamburguesa | **004** | gr |

**Pan bao:** en `BD_SUBRECETAS` el lote estándar es **15 uni** (`rendimiento_estandar`). En recetas de bao/hamburguesa (`cod_receta` **5** y afines) hoy figura **MP 50 × 2 uni** → migrar a **`cod_subreceta` 006 × 2 uni**, `cod_mp` vacío.

---

## 4. Matriz de trabajo (plantilla)

Copiar una fila por cambio planificado:

| cod_receta | variedad | acción | tipo | cod (MP o SUB) | nombre | cantidad | unidad | cod_bodega | notas |
|------------|----------|--------|------|----------------|--------|----------|--------|------------|-------|
| 017 | | QUITAR | MP | 567 | tocino | | | | pasa a SUB 003 |
| 017 | | AGREGAR | SUB | 003 | mermelada tocino | 30 | gr | BOD-001 | |
| 017 | | AGREGAR | SUB | 005 | cebollas caramelizadas | 20 | gr | BOD-001 | |
| 017 | | QUITAR | MP | 50 | PAN BAO | 2 | uni | | → SUB 006 |
| 017 | | AGREGAR | SUB | 006 | pan bao | 2 | uni | BOD-001 | |
| 017 | | MANTENER | SUB | 004 | carne hamburguesa | 150 | gr | BOD-001 | ya incluye chimi |
| 5 | * | QUITAR | MP | 50 | PAN BAO | 2 | uni | | todas las variedades bao |
| 5 | * | AGREGAR | SUB | 006 | pan bao | 2 | uni | BOD-001 | repetir por variedad si aplica |

---

## 5. Catálogo de subrecetas (referencia rápida)

Códigos en `BD_SUBRECETAS` (50 activas). Anidadas:

| Hijo → Padre | Uso en plato típico |
|--------------|---------------------|
| `023` → `004` | Usar **004** en hamburguesa, no chimi suelto |
| `021` → `022` | Usar **022** mayonesa verde, no aceite verde suelto |
| `036` → `037` | Usar **037** kimchi caramelizado, no kimchi `036` suelto |
| `016` → `017` | Usar **017** mayonesa ponzu en plato que la lleve |
| — | **006** pan bao | Usar **006** en bao/burger; no **MP 50** |

---

## 6. Formulario — `STAGING_RECETAS` (v2)

Hoja en spreadsheet de **staging**. Mary aprueba → promover a `BD_RECETAS_DETALLE`.

**Scripts:**

```bash
python setup_staging_recetas_v2.py              # layout MP + SUB
python setup_staging_recetas_v2.py --hoja STAGING_RECETAS_V2   # si convive con v1
python promover_staging_recetas.py --dry-run
python promover_staging_recetas.py --produccion
```

```bash
python setup_staging_subrecetas.py            # STAGING_SUB_CAB + STAGING_SUB_DETALLE
python promover_staging_subrecetas.py --dry-run
python promover_staging_subrecetas.py --produccion
```

Variable opcional: `STAGING_SPREADSHEET_ID` en `.env` (default: libro staging histórico).

**Apps Script:** `scripts_apps_script/tatami_staging.gs` — menú Tatami Admin en el libro staging (promover sin Python).

**Descargo por SUB (diseño):** `PLAN_DESCARGO_SUBRECETAS.md`.

### Columnas propuestas

| Col | Campo | Entrada |
|-----|--------|---------|
| A | `nombre_receta` | Texto |
| B | `cod_receta` | Código (ej. 208) |
| C | `variedad_smart_menu` | Texto o vacío |
| D | `tipo_linea` | Dropdown: **MP** \| **SUB** |
| E | `nombre_ingrediente` | Dropdown: MP desde `BD_MP_SISTEMA` o subreceta desde `BD_SUBRECETAS` |
| F | `cod_mp_sistema` | VLOOKUP si MP; vacío si SUB |
| G | `cod_subreceta` | VLOOKUP si SUB; vacío si MP |
| H | `cantidad` | Número |
| I | `unidad_base` | Auto: gr/ml/uni del maestro |
| J | `cod_bodega` | Dropdown BOD-001 / BOD-002 |
| K | `merma_pct` | Default 0 |
| L | `es_opcional` | SI / NO |
| M | `pct_aplicacion` | Default 100 |
| N | `estado` | PENDIENTE / APROBADO / RECHAZADO |

### Validación al promover

1. `tipo_linea` = MP → `cod_mp` lleno, `cod_subreceta` vacío.  
2. `tipo_linea` = SUB → `cod_subreceta` lleno, `cod_mp` vacío.  
3. `cod_subreceta` existe y `activa` = SI.  
4. Misma clave `(cod_receta, variedad, cod_mp|cod_sub)` no duplicada.

Filas aprobadas se escriben en **`BD_RECETAS_DETALLE`** con el mismo orden que §2 (`nombre_subreceta` antes de `cod_subreceta`).

### Formulario subrecetas (segundo track)

Pestañas **`STAGING_SUB_CAB`** y **`STAGING_SUB_DETALLE`** en staging (ver scripts arriba). Aprobación Mary → `BD_SUBRECETAS` / `BD_SUBRECETAS_DETALLE`.

---

## 7. Costos de subrecetas

| Dónde | Qué ves |
|-------|---------|
| **`BD_SUBRECETAS`** | `costo_lote_estandar`, `costo_unitario_estandar`, `costo_calc_at` |
| **Origen** | `costo_unitario_ref` en `BD_MP_SISTEMA` (promedio ponderado ENTRADAs por MP+bodega; ver `recalcular_stock_sheets.py`) |
| **Hijos** | Costo del padre incluye subrecetas hijas ya valoradas (ej. 037 usa costo de 036) |

```bash
python calcular_costo_subrecetas.py              # dry run
python calcular_costo_subrecetas.py --produccion # escribe en cabecera
```

**En plato:** línea SUB en `BD_RECETAS_DETALLE` → consumo × `costo_unitario_estandar` (misma unidad que `cantidad`: gr, ml, uni).

Fórmula Sheets (costo unitario en cabecera, opcional): igual que MPs — recalcular con script tras subir precios de compra.

---

## 7b. Costos de platos (recetas de venta)

| Dónde | Qué ves |
|-------|---------|
| **`BD_RECETAS`** | 1 fila por `(cod_receta, variedad_smart_menu)`: `costo_plato_estandar`, `n_lineas_mp`, `n_lineas_sub`, `lineas_sin_costo`, `costo_calc_at` |
| **Origen MP** | `costo_unitario_ref` en `BD_MP_SISTEMA` (misma bodega de la línea; fallback BOD-001/002/005) |
| **Origen SUB** | `costo_unitario_estandar` en `BD_SUBRECETAS` (ejecutar antes `calcular_costo_subrecetas.py`) |

Por línea en `BD_RECETAS_DETALLE` (por **1** plato vendido):

- **MP:** `cantidad × costo_unitario_ref × (1 + merma_pct) × pct_aplicacion`
- **SUB:** `cantidad × costo_unitario_estandar × pct_aplicacion`

```bash
python calcular_costo_subrecetas.py --produccion   # primero semis
python calcular_costo_recetas.py                   # dry run
python calcular_costo_recetas.py --produccion      # escribe BD_RECETAS
```

Tras subir precios de compra: `recalcular_stock_sheets.py` → subrecetas → platos.

Auditoría (platos inflados, MPs mal en receta): `python auditar_costos_recetas.py`  
WhatsApp: `costo_plato`, `auditar_costos_recetas`.

---

## 8. Comportamiento del sistema (hoy vs próximo)

| Función | Hoy | Tras producción de semis |
|---------|-----|---------------------------|
| **Descargo ventas** | Solo líneas **MP** (`descargo_inventario` ignora SUB) | Líneas **SUB** bajan stock del semi en `cod_bodega` |
| **PAR** | Explosión solo MPs en receta | Explosión recursiva SUB→MP (planeación) |
| **Auditoría recetas** | Manual + matriz §4 | `auditar_recetas_platos.py` (futuro) |

Hasta activar descargo SUB: puedes migrar el maestro sin romper descargo (sigue solo MPs).

---

## 8.1 Costos: ítem proveedor vs MP en recetas

| Dónde | Qué significa |
|-------|----------------|
| `BD_ITEMS_PROV` (`factor`, `precio_ref`) | Conversión **por código de ítem** al registrar una factura (ej. bloque facturado como 4000 g en una línea; otro ítem del mismo MP con 225 g). **No** es presentación “oficial” del MP. |
| `BD_MP_SISTEMA.costo_unitario_ref` | USD por `unidad_base` del MP para **recetas**: promedio ponderado de **ENTRADAs** de inventario (todas las compras de ese MP en la ventana). |
| Línea MP en plato/subreceta | `cantidad × costo_unitario_ref` (misma referencia para todo el MP; la `cod_bodega` de la línea es consumo, no otro costo). |

Ej. **MP 551** (queso crema): TARTA VASCA usa 94 g con el costo ref del 551; si en bodega solo entró bloque CON SAL a ~0,094 USD/g y también CLASICO a otro costo, el ref del 551 refleja el **mix de entradas**, no un factor 4000 g global.

Consulta por WhatsApp: `costo_plato` / `receta_ingredientes` (plato), `costo_subreceta` (semi).

---

## 9. Orden de trabajo recomendado

1. Completar **`BD_SUBRECETAS*`** (hecho; `python auditar_subrecetas.py`).  
2. Por plato prioritario (top ventas): llenar **matriz §4**.  
3. Editar **`BD_RECETAS_DETALLE`** (quitar MPs, agregar SUB).  
4. Actualizar **`STAGING_RECETAS`** v2 + script promover.  
5. Cuando exista producción: registrar lotes en **005** antes de confiar en stock de semis.  
6. Activar descargo por SUB en código.

---

## 10. Ejemplo completo — Hamburguesa `017`

**Antes (simplificado):** MP tocino, MP cebolla caramelizada, **MP 50 pan bao 2 uni**, MPs de carne sueltos, …

**Después:**

```
cod_receta | variedad | cod_subreceta | cod_mp | cantidad | unidad | bodega
017        |          | 003           |        | 30       | gr     | BOD-001
017        |          | 005           |        | 20       | gr     | BOD-001
017        |          | 004           |        | 150      | gr     | BOD-001
017        |          | 006           |        | 2        | uni    | BOD-001   ← pan bao (no MP 50)
5          | (c/u)    | 006           |        | 2        | uni    | BOD-001   ← bao: quitar MP 50 en todas las filas
…          |          |               | 079    | …        | gr     | BOD-001   ← MPs que siguen sueltos
```

No filas con tocino MP si `003` ya lo incluye. No fila chimi suelta si usas `004`. **No MP 50** si usas subreceta **006**.

---

*Documento de esquema operativo — Tatami Agente.*

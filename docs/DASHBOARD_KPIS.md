# Definiciones KPI — Dashboards Tatami

Documento de referencia para socios y operación. Versionado con el plan de dashboards 2026.

## Ventas

| KPI | Definición |
|-----|------------|
| Ventas netas | `subtotal - descuento_valor` por línea; excluye documentos anulados |
| Ticket promedio | Ventas netas ÷ unidades vendidas (por ítem de línea) |
| Mix Barra/Cocina | % participación por punto de venta según `BD_PRODUCTOS` |
| Comparativo | vs período anterior (misma duración) y vs mismo rango año anterior |

## Compras (pendiente)

| KPI | Definición |
|-----|------------|
| Compras registradas | Σ `mov_inventario.costo_total` donde `tipo_mov = ENTRADA` |
| Cobertura factura | % del total XML contabilizado vs `facturas_procesadas` PARCIAL |

## Rentabilidad (pendiente)

| KPI | Definición |
|-----|------------|
| Margen real | Venta neta − (consumo descargado × **costo promedio del período**) |
| Costo promedio período | Promedio ponderado de `costo_unitario` en ENTRADas del MP en el rango |
| Margen teórico | Venta neta − (uds × `costo_plato_estandar` de `BD_RECETAS`) |

## Inventario vivo — estados de stock

| Estado | Regla |
|--------|-------|
| OK | `stock ≥ par_level` |
| Bajo PAR | `50% ≤ stock/par < 100%` |
| Crítico (bajo) | `stock/par < 50%` |
| Quiebre / rotura | `stock ≤ 0` con consumo > 0 |
| Negativo | `stock < 0` |

### Sobre-stock (definición acordada)

| Nivel | Regla |
|-------|-------|
| **Alerta** | `stock ≥ 1,5 × par_level` |
| **Crítico (sobre-stock)** | Días de cobertura `≥ 2 × dias_cobertura_compra` |

**Días de cobertura** = `stock ÷ consumo_diario_calculado` (días que alcanza el stock).

**dias_cobertura_compra** = `dias_cobertura_par` del MP o ventana del proveedor en `BD_PROV` (ej. compra cada 15 días).

**Costo de oportunidad** = `(stock − par_level) × costo_unitario_ref` — capital inmovilizado sobre el PAR.

Mostrar siempre: días de cobertura actual, PAR en días, ratio stock/PAR.

## Roturas (histórico) — definición acordada

Incluye **ambos**:

1. **Rotura contable**: Σ `AJUSTE_NEGATIVO` en `mov_inventario` (conteos aprobados).
2. **Quiebre operativo**: días con `stock = 0` y `consumo_diario > 0` (requiere snapshot diario — fase 5b).

## Confianza de inventario (pendiente)

Score 0–100 por bodega:

- 30% frescura último conteo contabilizado
- 40% precisión delta conteo vs mov
- 20% drift Sheets vs mov
- 10% penalización stock negativo

-- Una vez en Supabase (SQL editor): columnas para estado de documento desde el grid Smart Menu.
-- Columna 9 del grid comprasloadVentas.php: vacío = venta activa, "ANULADO" = factura/ticket anulado.

alter table hist_ventas add column if not exists estado_documento text default 'ACTIVO';
alter table hist_ventas add column if not exists detalle_anulacion text;

comment on column hist_ventas.estado_documento is 'ACTIVO | ANULADO (grid Smart Menu)';
comment on column hist_ventas.detalle_anulacion is 'Texto motivo/anulación cuando aplica';

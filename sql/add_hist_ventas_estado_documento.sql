-- Una vez en Supabase (SQL editor): columnas para estado de documento desde el grid Smart Menu.
-- Columna 9 del grid: vacío = activa; ANULADO; NO_AUTORIZADO = nota/efectivo sin factura (sí opera en neto/descargo).

alter table hist_ventas add column if not exists estado_documento text default 'ACTIVO';
alter table hist_ventas add column if not exists detalle_anulacion text;

comment on column hist_ventas.estado_documento is 'ACTIVO | ANULADO | NO_AUTORIZADO (nota sin factura; cuenta en ventas/descargo)';
comment on column hist_ventas.detalle_anulacion is 'Texto motivo/anulación cuando aplica';

-- Meta / estado para confirmación de bodega en ingresos por factura.
-- Ejecutar en Supabase SQL Editor si la columna no existe.

alter table facturas_procesadas
  add column if not exists meta jsonb default '{}'::jsonb;

comment on column facturas_procesadas.meta is
  'JSON: pendiente_bodega[], bodegas_confirmadas, etc.';

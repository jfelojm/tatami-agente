-- Respaldo y deduplicación de comprobantes recibidos descargados del SRI.
-- Ejecutar en Supabase SQL Editor.

create table if not exists sri_comprobantes_recibidos (
  clave_acceso     text primary key,
  num_factura      text not null default '',
  ruc_emisor       text not null default '',
  razon_social     text not null default '',
  fecha_emision    date,
  xml_autorizado   text,
  fecha_descarga   timestamptz,
  fecha_proceso    timestamptz,
  estado           text not null default 'DESCARGADO'
    check (estado in ('DESCARGADO', 'PROCESADO', 'ERROR', 'OMITIDO')),
  meta             jsonb not null default '{}'::jsonb
);

create index if not exists idx_sri_comprobantes_recibidos_estado
  on sri_comprobantes_recibidos (estado);

create index if not exists idx_sri_comprobantes_recibidos_fecha_emision
  on sri_comprobantes_recibidos (fecha_emision desc);

comment on table sri_comprobantes_recibidos is
  'XML autorizados descargados del portal/WS SRI antes de procesar en facturas_procesadas.';

comment on column sri_comprobantes_recibidos.estado is
  'DESCARGADO=listo para procesar; PROCESADO=ingresado a inventario; ERROR=falló parseo/proceso; OMITIDO=ya existía en facturas_procesadas.';

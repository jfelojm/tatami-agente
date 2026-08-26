-- Congela categoría del maestro al snapshot del conteo.
-- Ejecutar una vez en el SQL Editor de Supabase.

alter table public.conteo_linea
  add column if not exists categoria text;

comment on column public.conteo_linea.categoria is
  'Categoría de BD_MP_SISTEMA al momento del snapshot (conteos ordenados/agrupados).';

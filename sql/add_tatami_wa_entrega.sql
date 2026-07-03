-- Estado WA + cola de alertas (digest local / webhook Railway comparten Supabase).
-- Ejecutar una vez en el SQL Editor de Supabase.

create table if not exists public.tatami_wa_contacto (
  wa_id text primary key,
  last_inbound_at timestamptz,
  template_enviada_at timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists public.tatami_wa_alertas_pendientes (
  id uuid primary key default gen_random_uuid(),
  wa_id text not null,
  cuerpo text not null,
  etiqueta text,
  origen text,
  creado_at timestamptz not null default now(),
  entregado_at timestamptz
);

create index if not exists idx_tatami_wa_alertas_pend_wa
  on public.tatami_wa_alertas_pendientes (wa_id)
  where entregado_at is null;

alter table public.tatami_wa_contacto enable row level security;
alter table public.tatami_wa_alertas_pendientes enable row level security;

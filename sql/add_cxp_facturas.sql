-- Control de pagos a proveedores (cuentas por pagar).
-- Una fila por factura recibida (SRI o manual). Cada factura se paga completa.
-- Ejecutar una vez en el SQL Editor de Supabase.

create table if not exists public.cxp_facturas (
  id                uuid primary key default gen_random_uuid(),
  num_factura       text not null,
  ruc_proveedor     text not null,
  razon_social      text not null default '',
  cod_proveedor     text,
  clave_acceso      text,
  fecha_emision     date,
  subtotal          numeric(14,2),
  iva               numeric(14,2),
  total             numeric(14,2),
  total_estimado    boolean not null default false,
  forma_pago_sri    text,
  plazo_xml         integer,
  dias_credito      integer not null default 0,
  fuente_credito    text not null default 'SIN_DATO'
    check (fuente_credito in ('BD_PROV', 'XML', 'SIN_DATO', 'MANUAL')),
  fecha_vencimiento date,
  estado_pago       text not null default 'PENDIENTE'
    check (estado_pago in ('PENDIENTE', 'PAGADA', 'ANULADA')),
  fecha_pago        date,
  medio_pago        text,
  referencia_pago   text,
  comprobante_url   text,
  lote_pago         text,
  pagado_por        text,
  pagado_at         timestamptz,
  notas             text,
  origen            text not null default 'SRI'
    check (origen in ('SRI', 'MANUAL')),
  creado_at         timestamptz not null default now(),
  actualizado_at    timestamptz not null default now(),
  unique (num_factura, ruc_proveedor)
);

create index if not exists idx_cxp_facturas_estado_venc
  on public.cxp_facturas (estado_pago, fecha_vencimiento);

create index if not exists idx_cxp_facturas_ruc
  on public.cxp_facturas (ruc_proveedor);

create table if not exists public.cxp_historial (
  id          uuid primary key default gen_random_uuid(),
  factura_id  uuid not null references public.cxp_facturas(id) on delete cascade,
  accion      text not null,
  usuario     text not null default '',
  detalle     jsonb not null default '{}'::jsonb,
  creado_at   timestamptz not null default now()
);

create index if not exists idx_cxp_historial_factura
  on public.cxp_historial (factura_id, creado_at desc);

alter table public.cxp_facturas enable row level security;
alter table public.cxp_historial enable row level security;

comment on table public.cxp_facturas is
  'Cuentas por pagar: estado de pago por factura. VENCIDA se calcula (PENDIENTE y fecha_vencimiento < hoy).';
comment on column public.cxp_facturas.fuente_credito is
  'BD_PROV=dias_credito/condicion del maestro; XML=plazo declarado en el XML SRI; SIN_DATO=contado por defecto; MANUAL=editado por usuario.';
comment on column public.cxp_facturas.total_estimado is
  'true cuando no hay XML (factura manual) y el total sale de la suma de líneas sin IVA.';

-- Inventario físico cíclico (Tatami) — modelo Supabase
-- Ejecutar en SQL Editor de Supabase (o migración).
-- Flujo: Iniciar conteo → snapshot en conteo_linea → Enviar → conteo_envio + conteo_envio_detalle
--        → aprobación Moisés → mov_inventario + recalcular_stock_sheets

-- ---------------------------------------------------------------------------
-- Ciclo: una planificación por semana/bodega (o archivo Sheets)
-- ---------------------------------------------------------------------------
create table if not exists conteo_ciclo (
  id uuid primary key default gen_random_uuid(),
  id_humano text unique,
  -- Periodo ISO semana: ej. 2026-W19
  anio smallint not null,
  semana_iso smallint not null check (semana_iso between 1 and 53),
  cod_bodega text not null,
  -- Etiqueta operativa: BARRA | COCINA | CONSIGNACION | BODEGA_ISRAEL (libre si usan otros códigos)
  area_etiqueta text,
  estado text not null default 'PLANIFICADO',
  spreadsheet_id text,
  sheet_name text default 'CONTEO',
  responsable_nombre text,
  responsable_contacto text,
  snapshot_at timestamptz,
  notas text,
  meta jsonb default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint conteo_ciclo_estado_chk check (
    estado in (
      'PLANIFICADO',
      'SNAPSHOT_LISTO',
      'BORRADOR_CONTEO',
      'CONTABILIZADO',
      'ANULADO'
    )
  )
);

create index if not exists idx_conteo_ciclo_periodo_bodega
  on conteo_ciclo (anio, semana_iso, cod_bodega);
create index if not exists idx_conteo_ciclo_estado on conteo_ciclo (estado);

comment on table conteo_ciclo is 'Ciclo de inventario físico (semana × bodega). Snapshot al iniciar conteo.';
comment on column conteo_ciclo.id_humano is 'Opcional: INV-2026-W19-BOD-001';
comment on column conteo_ciclo.estado is 'PLANIFICADO → SNAPSHOT_LISTO → BORRADOR_CONTEO → CONTABILIZADO | ANULADO';

-- ---------------------------------------------------------------------------
-- Líneas de trabajo del ciclo (una fila por MP × bodega). Rellenadas al Iniciar.
-- conteo_fisico NULL = aún no capturado en backend; en Sheets debe estar lleno antes de Enviar.
-- ---------------------------------------------------------------------------
create table if not exists conteo_linea (
  id uuid primary key default gen_random_uuid(),
  ciclo_id uuid not null references conteo_ciclo (id) on delete cascade,
  line_no integer,
  cod_mp_sistema text not null,
  cod_bodega text not null,
  nombre_mp text,
  unidad_base text,
  stock_sistema_snapshot numeric(18, 6),
  costo_unitario_ref_snapshot numeric(18, 8),
  snapshot_at timestamptz,
  conteo_fisico numeric(18, 6),
  notas text,
  delta_calculado numeric(18, 6) generated always as (
    case
      when conteo_fisico is null or stock_sistema_snapshot is null then null
      else conteo_fisico - stock_sistema_snapshot
    end
  ) stored,
  valor_delta_estimado numeric(18, 4) generated always as (
    case
      when conteo_fisico is null
        or stock_sistema_snapshot is null
        or costo_unitario_ref_snapshot is null then null
      else (conteo_fisico - stock_sistema_snapshot) * costo_unitario_ref_snapshot
    end
  ) stored,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (ciclo_id, cod_mp_sistema, cod_bodega)
);

create index if not exists idx_conteo_linea_ciclo on conteo_linea (ciclo_id);
create index if not exists idx_conteo_linea_cod_mp on conteo_linea (cod_mp_sistema);

comment on table conteo_linea is 'Líneas del ciclo: snapshot al iniciar; conteo_fisico al capturar (sync desde Sheets o API).';
comment on column conteo_linea.conteo_fisico is 'Obligatorio numérico al enviar (0 válido). NULL permitido en borrador.';

-- ---------------------------------------------------------------------------
-- Cada envío exitoso (incluye correcciones: secuencia 1, 2, …)
-- ---------------------------------------------------------------------------
create table if not exists conteo_envio (
  id uuid primary key default gen_random_uuid(),
  ciclo_id uuid not null references conteo_ciclo (id) on delete cascade,
  secuencia integer not null default 1,
  enviado_at timestamptz not null default now(),
  enviado_por text,
  enviado_por_contacto text,
  payload_hash text,
  observaciones text,
  estado_aprobacion text not null default 'PENDIENTE_REVISION',
  aprobado_at timestamptz,
  aprobado_por text,
  contabilizado_at timestamptz,
  meta jsonb default '{}'::jsonb,
  unique (ciclo_id, secuencia),
  constraint conteo_envio_aprob_chk check (
    estado_aprobacion in (
      'PENDIENTE_REVISION',
      'APROBADO_TOTAL',
      'APROBADO_PARCIAL',
      'RECHAZADO',
      'CONTABILIZADO'
    )
  )
);

create index if not exists idx_conteo_envio_ciclo on conteo_envio (ciclo_id);
create index if not exists idx_conteo_envio_estado on conteo_envio (estado_aprobacion);

comment on table conteo_envio is 'Registro inmutable por cada Enviar exitoso. Corrección = nuevo envío con secuencia+1.';
comment on column conteo_envio.estado_aprobacion is 'Moisés: PENDIENTE → APROBADO_* / RECHAZADO → CONTABILIZADO tras mov_inventario.';

-- ---------------------------------------------------------------------------
-- Detalle congelado del envío (auditoría; no editar tras insert)
-- ---------------------------------------------------------------------------
create table if not exists conteo_envio_detalle (
  id uuid primary key default gen_random_uuid(),
  envio_id uuid not null references conteo_envio (id) on delete cascade,
  line_no integer,
  cod_mp_sistema text not null,
  cod_bodega text not null,
  nombre_mp text,
  unidad_base text,
  stock_sistema_snapshot numeric(18, 6) not null,
  costo_unitario_ref_snapshot numeric(18, 8),
  conteo_fisico numeric(18, 6) not null,
  delta_calculado numeric(18, 6) not null,
  valor_delta_estimado numeric(18, 4),
  notas text,
  estado_linea text not null default 'PENDIENTE_APROBACION',
  cod_mov_ajuste text,
  created_at timestamptz not null default now(),
  constraint conteo_envio_detalle_linea_chk check (
    estado_linea in (
      'PENDIENTE_APROBACION',
      'APROBADO',
      'RECHAZADO',
      'CONTABILIZADO'
    )
  )
);

create index if not exists idx_conteo_envio_det_envio on conteo_envio_detalle (envio_id);
create index if not exists idx_conteo_envio_det_mp on conteo_envio_detalle (cod_mp_sistema);

comment on table conteo_envio_detalle is 'Copia al momento del envío. Aprobación y cod_mov_ajuste por línea.';
comment on column conteo_envio_detalle.cod_mov_ajuste is 'FK lógica a mov_inventario.cod_mov tras contabilizar.';

-- ---------------------------------------------------------------------------
-- updated_at automático (nombre único para no chocar con otros módulos)
-- ---------------------------------------------------------------------------
create or replace function tatami_conteo_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_conteo_ciclo_updated on conteo_ciclo;
create trigger trg_conteo_ciclo_updated
  before update on conteo_ciclo
  for each row execute procedure tatami_conteo_touch_updated_at();

drop trigger if exists trg_conteo_linea_updated on conteo_linea;
create trigger trg_conteo_linea_updated
  before update on conteo_linea
  for each row execute procedure tatami_conteo_touch_updated_at();


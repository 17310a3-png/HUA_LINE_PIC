-- ============================================================
-- LINE 貼圖自動化 - Supabase 初始化 Schema
-- Phase 1 MVP：series / jobs / sheets + 3 天自動清除
-- ============================================================

-- ------------------------------------------------------------
-- 1. 文案系列（從本機 文案整理/*.md 匯入）
-- ------------------------------------------------------------
create table if not exists public.series (
  id text primary key,                      -- e.g. 'office', 'daily'
  name text not null,                       -- '上班系列'
  items jsonb not null,                     -- [{idx:1, text:'...', action:'...'}, ...]
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- ------------------------------------------------------------
-- 2. 套組任務
-- ------------------------------------------------------------
create table if not exists public.jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users on delete cascade not null,
  set_name text not null,
  character_name text,
  character_prompt text,
  reference_image_path text,                -- storage key
  series_id text references public.series,
  total int not null check (total in (8, 16, 24, 32, 40)),
  model text not null default 'flash' check (model in ('flash', 'pro')),
  status text not null default 'pending'
    check (status in ('pending','generating','review','finalizing','done','expired','rejected','failed')),
  zip_path text,
  error text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  expires_at timestamptz default (now() + interval '3 days')
);

create index if not exists idx_jobs_user_created on public.jobs(user_id, created_at desc);
create index if not exists idx_jobs_expires on public.jobs(expires_at) where status in ('done','review');

-- ------------------------------------------------------------
-- 3. 每張 3×3 sheet 原稿
-- ------------------------------------------------------------
create table if not exists public.sheets (
  id uuid primary key default gen_random_uuid(),
  job_id uuid references public.jobs on delete cascade not null,
  sheet_number int not null check (sheet_number between 1 and 6),
  storage_path text not null,
  created_at timestamptz default now(),
  unique(job_id, sheet_number)
);

-- ------------------------------------------------------------
-- 4. GPT 文案草稿（Phase 1.5）
-- ------------------------------------------------------------
create table if not exists public.series_drafts (
  id uuid primary key default gen_random_uuid(),
  series_id text references public.series,
  items jsonb not null,
  source text not null,
  generated_at timestamptz default now(),
  reviewed boolean default false,
  approved boolean default false
);

-- ------------------------------------------------------------
-- 5. updated_at 自動更新 trigger
-- ------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at := now(); return new; end;
$$;

drop trigger if exists trg_jobs_updated on public.jobs;
create trigger trg_jobs_updated before update on public.jobs
  for each row execute function public.set_updated_at();

drop trigger if exists trg_series_updated on public.series;
create trigger trg_series_updated before update on public.series
  for each row execute function public.set_updated_at();

-- ------------------------------------------------------------
-- 6. Storage bucket（private，靠 signed URL）
-- ------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('sticker-assets', 'sticker-assets', false)
on conflict (id) do nothing;

-- ------------------------------------------------------------
-- 7. pg_cron 3 天自動清除（需 pg_cron + pg_net extension）
-- 先在 Supabase Dashboard → Database → Extensions 啟用 pg_cron 與 pg_net，
-- 然後重跑這份 SQL；若未啟用此區段會被跳過（不影響前面 6 個區段）。
-- ------------------------------------------------------------
do $$
declare
  has_cron boolean;
begin
  select exists(select 1 from pg_extension where extname = 'pg_cron') into has_cron;
  if not has_cron then
    raise notice '  [skip] pg_cron 未啟用，略過排程設定';
    return;
  end if;

  -- 先解除舊排程（若重跑 migration）
  begin
    perform cron.unschedule('cleanup-expired-jobs');
  exception when others then null;
  end;

  perform cron.schedule(
    'cleanup-expired-jobs',
    '0 * * * *',
    $sql$
      select net.http_post(
        url := current_setting('app.cleanup_url', true),
        headers := jsonb_build_object(
          'Authorization', 'Bearer ' || current_setting('app.cleanup_token', true),
          'Content-Type', 'application/json'
        ),
        body := '{}'::jsonb
      );

      update public.jobs
        set status='expired', zip_path=null
        where expires_at < now() and status in ('done','review');
    $sql$
  );
  raise notice '  [ok] cron 排程已建立';
end $$;

-- 設定 cleanup 目標（啟用 pg_cron 後執行一次，填入你的 FastAPI URL 和內部 token）：
-- alter database postgres set app.cleanup_url = 'https://hua-line-pic.zeabur.app/internal/cleanup';
-- alter database postgres set app.cleanup_token = '<INTERNAL_CLEANUP_TOKEN>';

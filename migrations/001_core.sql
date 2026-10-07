-- 001_core: shared types, projects, agents (docs/ARCHITECTURE.md §4, §11, §12)
-- Extensions vector/pg_trgm/unaccent are pre-created by initdb as superuser.

-- tenant mirrors the Nitro (work) / Pessoal (personal) isolation; 'shared' = global prefs/rules.
CREATE TYPE tenant AS ENUM ('nitro', 'pessoal', 'shared');
-- agent domains (§4): one agent per domain, projects are context.
CREATE TYPE domain AS ENUM ('chief', 'engineering', 'finance', 'projects', 'personal', 'learning');

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS
$$BEGIN NEW.updated_at = now(); RETURN NEW; END$$;

CREATE TABLE projects (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text UNIQUE NOT NULL,
  name        text NOT NULL,
  tenant      tenant NOT NULL,
  domain      domain NOT NULL DEFAULT 'engineering',
  description text,
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
  repository  text,                                   -- git remote or storage:// ref, never a host path
  metadata    jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER projects_upd BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE agents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug          text UNIQUE NOT NULL,
  name          text NOT NULL,
  domain        domain NOT NULL,
  runtime       text NOT NULL DEFAULT 'hermes',
  default_model text NOT NULL DEFAULT 'tier3-code',   -- LiteLLM alias
  max_tier      smallint NOT NULL DEFAULT 5 CHECK (max_tier BETWEEN 0 AND 7),
  max_cost_per_run_usd numeric(10, 4) NOT NULL DEFAULT 0.50,
  config        jsonb NOT NULL DEFAULT '{}',          -- mirror of agents/<slug>/agent.yaml
  enabled       boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER agents_upd BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 003_permissions: explicit per-agent scope of data and tools (§12, principle: least privilege)
-- Evaluation order: deny > ask > allow; no match = deny.

CREATE TABLE permissions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id      uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  resource_type text NOT NULL CHECK (resource_type IN ('domain', 'tenant', 'tool', 'path', 'model', 'project', 'external_api')),
  resource      text NOT NULL,                        -- exact or glob: 'finance', 'shell', '/workspace/**', 'tier6-*'
  action        text NOT NULL DEFAULT '*' CHECK (action IN ('read', 'write', 'execute', 'spend', '*')),
  effect        text NOT NULL CHECK (effect IN ('allow', 'deny', 'ask')),
  conditions    jsonb NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (agent_id, resource_type, resource, action)
);

-- human approval queue for 'ask' effects and critical actions (§26 Segurança)
CREATE TABLE approvals (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id      uuid REFERENCES agent_runs(id) ON DELETE CASCADE,
  agent_id    uuid REFERENCES agents(id),
  request     jsonb NOT NULL,
  status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
  decided_by  text,
  decided_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX approvals_pending ON approvals (created_at) WHERE status = 'pending';

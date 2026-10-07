-- 004_knowledge: documents + chunks (hybrid pgvector + Portuguese full text) and layered memory (§10, §11)
-- Embedding: qwen3-embedding:0.6b via LiteLLM alias 'embed-local' -> vector(1024).
-- Changing the embedding model means re-embedding (embedding_model column tracks it).

CREATE TEXT SEARCH CONFIGURATION pt_unaccent (COPY = portuguese);
ALTER TEXT SEARCH CONFIGURATION pt_unaccent
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, portuguese_stem;

CREATE TABLE documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant        tenant NOT NULL,
  domain        domain,
  project_id    uuid REFERENCES projects(id) ON DELETE SET NULL,
  source_type   text NOT NULL CHECK (source_type IN ('obsidian', 'file', 'url', 'audio', 'video', 'manual', 'agent')),
  source_uri    text NOT NULL,                        -- abstract ref: obsidian://Pessoal/03 Áreas/x.md, storage://documents/abc.pdf (§24.11)
  title         text,
  mime_type     text,
  lang          text NOT NULL DEFAULT 'pt',
  content_hash  text NOT NULL,                        -- sha256 of normalized content; unchanged => skip re-embed
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deprecated', 'deleted')),
  metadata      jsonb NOT NULL DEFAULT '{}',          -- frontmatter, tags, links, timestamps
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant, source_uri)
);
CREATE INDEX documents_tenant_domain ON documents (tenant, domain) WHERE status = 'active';
CREATE INDEX documents_metadata ON documents USING gin (metadata jsonb_path_ops);
CREATE TRIGGER documents_upd BEFORE UPDATE ON documents FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE chunks (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index     int NOT NULL,
  heading_path    text,                               -- "H1 > H2" breadcrumb for context
  content         text NOT NULL,
  token_count     int,
  embedding       vector(1024),
  embedding_model text NOT NULL DEFAULT 'qwen3-embedding:0.6b',
  tsv             tsvector GENERATED ALWAYS AS (to_tsvector('pt_unaccent'::regconfig, coalesce(heading_path, '') || ' ' || content)) STORED,
  metadata        jsonb NOT NULL DEFAULT '{}',
  UNIQUE (document_id, chunk_index)
);
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);

-- Memory layers (§11): global (tenant=shared, no domain) / domain / project, plus episodic lifecycle.
CREATE TABLE memories (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant         tenant NOT NULL,
  scope          text NOT NULL CHECK (scope IN ('global', 'domain', 'project')),
  domain         domain,
  project_id     uuid REFERENCES projects(id) ON DELETE CASCADE,
  agent_id       uuid REFERENCES agents(id) ON DELETE SET NULL,
  kind           text NOT NULL CHECK (kind IN ('preference', 'working_style', 'communication', 'rule',
                                               'fact', 'decision', 'architecture', 'roadmap', 'constraint',
                                               'issue', 'episode', 'procedure')),
  lifecycle      text NOT NULL DEFAULT 'important' CHECK (lifecycle IN ('temporary', 'important', 'persistent', 'deprecated')),
  content        text NOT NULL,
  embedding      vector(1024),
  embedding_model text,
  importance     real NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  source_document_id uuid REFERENCES documents(id) ON DELETE SET NULL,
  source_run_id  uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  superseded_by  uuid REFERENCES memories(id),
  expires_at     timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (scope <> 'domain'  OR domain IS NOT NULL),
  CHECK (scope <> 'project' OR project_id IS NOT NULL)
);
CREATE INDEX memories_embedding_hnsw ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX memories_live ON memories (tenant, scope, domain, kind)
  WHERE superseded_by IS NULL AND lifecycle <> 'deprecated';
CREATE TRIGGER memories_upd BEFORE UPDATE ON memories FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Read-only role for retrieval (Hermes MCP / context compiler): SELECT on knowledge only.
GRANT USAGE ON SCHEMA public TO aios_reader;
GRANT SELECT ON documents, chunks, memories, projects TO aios_reader;

---
name: repository_analysis
description: "Mapear repositório: stack, comandos, entrypoints, riscos."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: coding
    tags: [coding, repository, onboarding, deterministic, sandbox]
    related_skills: [debugging, code_review, architecture_review, test_generation]
    requires_toolsets: [terminal]
---

# Repository Analysis

Produz um mapa factual de um repositório antes de planejar ou editar: linguagens, manifests, comandos
de build/teste/lint, entrypoints, árvore, histórico recente e marcadores pendentes. O mapa sai de um
script determinístico (zero tokens); o LLM só interpreta. Não altera nada no repositório.

## When to Use

- Primeira tarefa num repositório (ou num pacote de um monorepo) nesta sessão.
- Antes de implementar, depurar, refatorar ou revisar quando não se sabe como o projeto roda.
- Quando o roteamento (`[aios] ... Skills sugeridas`) indicar `coding/repository_analysis`.

Não use para perguntas sobre um único arquivo já conhecido: leia o arquivo com `read_file`.

## Inputs

- Caminho do repositório no sandbox (padrão `/workspace/tasks/<id>` criado por `aios-task-start`).
- Tenant da tarefa (`nitro` = trabalho, `pessoal` = projetos pessoais) e, se houver, o slug do projeto.

## Procedure

1. **Workspace.** Se o repositório ainda não está no sandbox, peça/crie via `terminal`:
   ```bash
   aios-task-start <task-id> <git-url> [ref]      # clona em /workspace/tasks/<task-id>, branch aios/<task-id>
   ```
2. **Mapa determinístico** (via `terminal`):
   ```bash
   D="${HERMES_SKILL_DIR}"; [ -d "$D" ] || D=$(ls -d ~/.hermes/external_skills/*/coding/repository_analysis 2>/dev/null | head -n 1)
   bash "$D/scripts/repo_map.sh" --format md /workspace/tasks/<task-id>
   bash "$D/scripts/repo_map.sh" /workspace/tasks/<task-id> > /workspace/tasks/<task-id>.map.json   # JSON p/ reuso
   ```
   Em monorepo, rode de novo no pacote alvo (ex.: `.../services/api`): os comandos são detectados pelos
   manifests da raiz informada.
3. **Contexto existente.** Antes de ler código, busque o que já se sabe (sempre com `tenant`):
   - `mcp__knowledge__compile_context(task="<objetivo>", tenant=<tenant>, domain="engineering", project=<slug>)`
   - `mcp__knowledge__memory_search(tenant=<tenant>, query="[projeto <slug>] arquitetura decisões", kind="architecture")`
     (sem filtro `project`: memórias de projeto não cadastrado ficam em `scope="domain"`)
4. **Confirme os comandos.** Rode o comando de teste detectado uma vez para ter a linha de base:
   ```bash
   aios-check --json /workspace/tasks/<task-id>        # {exit_code, output_tail, command}
   ```
   Se `commands.test` veio vazio ou o JSON tem `skipped: true` (nenhum comando detectado), procure no
   README/CI (`commands.ci`), rode o comando real e registre-o (passo 6).
5. **Leitura dirigida.** Com `search_files` e `read_file`, leia só: entrypoints, o módulo do objetivo e
   seus testes. Não leia o repositório inteiro.
6. **Síntese** no formato de saída abaixo. Se descobriu fatos duráveis (comando de teste não óbvio,
   convenção, armadilha), salve com `mcp__knowledge__memory_save(tenant=<tenant>, scope="project",
   kind="procedure", project=<slug>, domain="engineering", content="[projeto <slug>] ...")`.
   Erro "unknown project" (projeto não cadastrado no knowledge) → repita com `scope="domain"` e o mesmo `content`, que começa com `[projeto <slug>]`.

## Outputs

```markdown
## Repositório <nome> (<branch>@<sha>)
- Stack: <linguagens/frameworks>   · Tamanho: <n> arquivos   · Atividade: <commits 30d>
- Rodar: build `<cmd>` · teste `<cmd>` (linha de base: <passa|falha N>) · lint `<cmd>`
- Entrypoints: <lista curta>
- Módulos relevantes para "<objetivo>": <arquivos e por quê>
- Riscos/hot spots: <marcadores pendentes, áreas sem teste, arquivos sujos>
- Próximo passo: <skill seguinte: debugging | test_generation | code_review | ...>
```

## Gate e escalonamento

- O modelo é escolhido pelo Decision Service (plugin `aios`). Não peça para trocar de modelo.
- Esta skill não edita código; a validação por testes acontece nas skills de implementação, via gate.
- Ao ver "orçamento esgotado", não chame mais ferramentas: entregue o mapa que já tem.

## Pitfalls

- `repo_map.sh` respeita `.gitignore`; arquivos gerados não aparecem (correto para análise).
- Comandos detectados são heurísticos: confirme no passo 4 antes de confiar.
- Nunca leia `.env`, chaves ou credenciais (bloqueado pelo plugin); use `.env.example`.
- Se o script não existir no caminho do sandbox, o fallback do passo 2 procura a cópia sincronizada
  em `~/.hermes/external_skills/`.

## Verification

- O mapa JSON é válido (`python3 -m json.tool < <task-id>.map.json`) e a linha de base de testes foi
  registrada com o comando exato.

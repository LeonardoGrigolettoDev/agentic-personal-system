---
name: debugging
description: "Depurar bug ou teste falhando: reproduzir, isolar, corrigir."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: coding
    tags: [coding, debugging, tests, repair-loop, sandbox]
    related_skills: [repository_analysis, test_generation, code_review]
    requires_toolsets: [terminal]
---

# Debugging

Loop disciplinado de diagnóstico e reparo (ARCHITECTURE §7): reproduzir → isolar → hipótese →
corrigir → validar. Toda conclusão vem de evidência executada no sandbox, não de leitura especulativa.
O gate do plugin `aios` decide repair/escalate/done; a skill não troca de modelo nem "tenta de novo"
sem informação nova.

## When to Use

- Teste falhando, stack trace, erro em produção/log, comportamento divergente do esperado.
- Roteamento com `task_type=debugging` ou mensagem `[aios gate: repair]` com diagnóstico.

Não use para features novas (implemente e use `test_generation`) nem para revisão de diff (`code_review`).

## Inputs

- Sintoma: mensagem de erro, teste, log ou passo a passo; versão/commit onde ocorre.
- Repositório no sandbox (`/workspace/tasks/<id>`) e tenant da tarefa (`nitro` | `pessoal`).

## Procedure

1. **Contexto barato primeiro.**
   - Mapa do repo se ainda não houver: skill `repository_analysis`.
   - Bugs parecidos já vistos: `mcp__knowledge__memory_search(tenant=<tenant>, query="<mensagem de erro>",
     domain="engineering", kind="issue")` e `mcp__knowledge__knowledge_search(tenant=<tenant>, query=...)`.
2. **Reproduzir de forma determinística** (via `terminal`). Rode o menor comando que mostra a falha:
   ```bash
   cd /workspace/tasks/<id>
   go test ./pkg/x -run TestY -count=1          # ou: uv run pytest -q tests/test_x.py::test_y -x
   ```
   Sem reprodução não há correção: se não reproduz, colete mais dados (versão, entrada, ambiente) e
   pare com um relatório em vez de adivinhar.
3. **Fixar o sintoma em teste.** Se não existe teste que falha, escreva o menor teste que reproduz o
   bug (veja `test_generation`) e confirme que ele falha pelo motivo certo.
4. **Isolar.** Reduza o espaço de busca com evidência, nesta ordem:
   - stack trace → arquivo/linha; `search_files` pelo símbolo; `read_file` só das funções envolvidas;
   - `git log -p -n 5 -- <arquivo>` e `git bisect` quando "funcionava antes";
   - prints/logs temporários ou depurador; entrada mínima que ainda falha.
5. **Hipótese explícita.** Escreva em uma frase: "falha porque X; se eu mudar Y, o teste Z passa e nada
   mais quebra". Uma hipótese por vez.
6. **Correção mínima** com `patch`/`write_file`: corrija a causa, não o sintoma; sem refatoração
   oportunista. Remova logs temporários.
7. **Validar.** Rode o teste do bug e a suíte do pacote; depois a validação completa:
   ```bash
   aios-check --json /workspace/tasks/<id>       # {exit_code, output_tail, command}
   ```
   Ao terminar a edição, o hook `pre_verify` do `aios` roda o mesmo check e consulta o gate.
8. **Registrar.** Se a causa for não óbvia ou recorrente, salve:
   `mcp__knowledge__memory_save(tenant=<tenant>, scope="project", project=<slug>, domain="engineering",
   kind="issue", lifecycle="important", content="[projeto <slug>] <sintoma> → <causa> → <correção>
   (<commit>)")`. Erro "unknown project" (projeto não cadastrado no knowledge) → repita com `scope="domain"` e o mesmo `content`, que começa com `[projeto <slug>]`.
   Gere o patch com `aios-task-patch <id>` quando a tarefa pedir entrega por patch.

## Gate, orçamento e escalonamento

- `[aios gate: repair] <diagnóstico>`: continue no mesmo modelo usando o diagnóstico; mude a hipótese,
  não repita a mesma correção.
- `[aios gate: escalate]`: o Decision Service já trocou o modelo; recomece do passo 5 com o histórico.
- `[aios gate: fail]` ou `ask_human`, ou orçamento esgotado: pare, não execute novas ferramentas e
  entregue relatório (hipóteses testadas, evidência, próximo passo) via `kanban_block` ou resposta.
- Nunca peça para trocar de modelo; o escalonamento é por evidência (falhas repetidas, baixa confiança).

## Outputs

```markdown
**Causa:** <uma frase, com arquivo:linha>
**Correção:** <o que mudou e por quê> · arquivos: <lista>
**Evidência:** `<comando>` antes: falha (<erro>) · depois: passa · suíte: <n> ok
**Riscos/pendências:** <efeitos colaterais, testes faltantes>
```

## Pitfalls

- Corrigir o teste em vez do código (ou relaxar asserções) só para ficar verde: proibido sem
  aprovação explícita.
- Bugs intermitentes: rode o teste N vezes (`-count=20`, `pytest -x --count` se houver plugin) antes de
  declarar corrigido.
- Não rode comandos destrutivos (reset de banco, `rm -rf` amplo, push forçado); o plugin bloqueia e
  exige aprovação humana.
- Erros de ambiente (dependência ausente, rede) não são bugs do código: reporte e bloqueie a tarefa.

## Verification

- O teste que reproduzia o bug falha no commit original e passa com a correção; `aios-check` retorna
  `exit_code: 0`.

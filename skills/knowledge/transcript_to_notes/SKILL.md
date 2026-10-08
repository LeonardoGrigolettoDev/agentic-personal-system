---
name: transcript_to_notes
description: "Transcrever áudio/vídeo no edge e virar notas e tarefas."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
required_environment_variables:
  - name: EDGE_API_KEY
    prompt: "Chave da API do edge (EDGE_API_KEY do .env)"
    help: "Gerada por `make env` no repositório agent-system"
    required_for: "chamar o pipeline de mídia do edge pelo terminal do sandbox"
  - name: HERMES_TENANT
    prompt: "Tenant da sessão (definido pelo Hermes em workers Kanban)"
    help: "Opcional: o script recusa TENANT diferente dele"
    required_for: "impedir que o pipeline grave em outro tenant"
    optional: true
metadata:
  hermes:
    category: knowledge
    tags: [knowledge, media, transcription, whisper, notes, edge]
    related_skills: [knowledge_capture, source_synthesis, decision_record, daily_review]
---

# Transcript to Notes

Transforma aula, reunião, áudio de WhatsApp ou vídeo em transcrição + resumo + notas + tarefas,
processado localmente pelo serviço `edge` (ffmpeg → whisper → Qwen local) e ingerido na base de
conhecimento do tenant certo. O LLM da sessão só revisa o resultado e decide o que vira tarefa ou
decisão — a parte pesada não gasta tokens de nuvem.

## When to Use

- "Transcreva/resuma esta aula/reunião/áudio/vídeo", arquivo de mídia novo em `storage://media/input/`.
- Transcrição em texto já existente (`.txt`/`.srt`) que precisa virar notas e tarefas (pule o passo 2).

## Inputs

- Fonte: URI `storage://media/input/<arquivo>` (upload feito pelo usuário ou por `make transcribe`),
  ou arquivo local no sandbox (use `--upload`).
- Tenant (`pessoal` para aulas/vida pessoal, `nitro` para reuniões de trabalho) e domínio
  (`learning`, `projects`, `engineering`, …); título opcional.

## How to Run

Pelo `terminal` (sandbox). O script envia o job assíncrono, acompanha até terminar e imprime o JSON
do resultado. Transcrições longas levam minutos: rode em segundo plano com notificação
(`terminal(command=..., background=true, notify=true)`).

```bash
D="${HERMES_SKILL_DIR}"; [ -d "$D" ] || D=$(ls -d ~/.hermes/external_skills/*/knowledge/transcript_to_notes 2>/dev/null | head -n 1)
bash "$D/scripts/edge_pipeline.sh" --domain learning --title "Aula 05 — Redes" storage://media/input/aula05.m4a pessoal
bash "$D/scripts/edge_pipeline.sh" --upload /workspace/inbox/reuniao.mp4 --domain projects nitro
bash "$D/scripts/edge_pipeline.sh" --job <job_id>          # retoma a espera após timeout (exit 6)
```

Saída: `txt_uri`, `srt_uri`, `summary_uri`, `summary` (resumo, notas, tarefas, tópicos), `ingest[]`
(documentos gravados no knowledge) e `warnings`. Códigos de saída: 3 sem `EDGE_API_KEY`, 4 erro do
edge, 5 edge inacessível, 6 tempo esgotado (o job continua), 7 tenant diferente do `HERMES_TENANT` da
sessão (crie a tarefa no tenant certo; não troque o argumento). Exit 3 com a chave configurada no Hermes
significa que o `sshd` do sandbox não aceita a variável (`AcceptEnv EDGE_API_KEY`): delegue ao usuário
(`make transcribe`) e avise.

Sem `terminal` nesta sessão (chief, learning, personal): delegue com
`kanban_create(title="Transcrever <arquivo>", assignee="engineering", tenant=<tenant>,
skills=["transcript_to_notes"], body="fonte storage://…, domínio …, título …")` — o agente de
engenharia tem o shell do sandbox. No host, o usuário também pode rodar `make transcribe f=<arquivo>`.

## Procedure

1. Confirme tenant e domínio antes de processar: o edge grava a transcrição na base desse tenant.
2. Rode o pipeline (acima). Para mídia sem fala útil (`warnings: nenhuma fala detectada`), pare e avise.
3. Revise o `summary`: corrija nomes próprios e termos técnicos óbvios; não invente o que não está
   na transcrição. Para detalhes, leia trechos do `.txt`/`.srt` via resultado de busca
   `mcp__knowledge__knowledge_search(tenant=<tenant>, query="<tema>", domain=<domínio>)`.
4. **Notas** (aulas/estudos): se o resumo do edge for raso, gere notas estruturadas e grave com
   `mcp__knowledge__ingest_note(tenant=<tenant>, title="Notas: <título> (<data>)", content=..., domain=<domínio>,
   source_uri="agent://notes/<domínio>/<slug-do-título>")` seguindo `knowledge_capture` (conceitos-chave
   + perguntas de revisão).
5. **Tarefas** extraídas: apresente a lista (o quê, dono, prazo citado). Com aprovação, crie
   `kanban_create(assignee=<domínio>, tenant=<tenant>, title=..., body="Origem: <título> <timestamp>")`.
6. **Decisões** tomadas na reunião → skill `decision_record`. Fatos duráveis → `knowledge_capture`.
7. Responda com o resumo final, links `storage://` (txt/srt/resumo) e o que foi criado.

## Outputs

```markdown
## <título> (<duração>, <tenant>/<domínio>)
**Resumo:** …
**Notas:** <tópicos com bullets>
**Tarefas propostas:** 1) … (dono, prazo) — criar? 
**Decisões:** …
Arquivos: transcrição storage://… · legendas storage://… · resumo storage://…
```

## Gate e escalonamento

- Não há validador automático para transcrição (o gate `aios` só roda quando código é editado): a
  evidência é o JSON do edge (`txt_uri`, `ingest[]`, `warnings`) e trechos da transcrição citados.
- Nunca peça outro modelo para "melhorar" a transcrição: o resumo pesado é do Qwen local no edge.
- Ao ver "orçamento esgotado", não chame mais ferramentas: entregue os URIs e o resumo já obtidos.

## Pitfalls

- Tenant errado vaza conteúdo: reunião de trabalho nunca em `pessoal`. O tenant vai como argumento do
  script, que o guard `aios` não inspeciona; por isso o script recusa tenant diferente de `HERMES_TENANT`
  (sessões sem essa variável não têm essa proteção: confira o tenant antes de rodar).
- Carregar esta skill registra `EDGE_API_KEY` para o terminal da sessão: todo comando seguinte (inclusive
  testes de repositório não confiável) a recebe, e o edge não restringe tenant por chave. Use a skill
  só em tarefas dedicadas de transcrição, nunca numa sessão que roda código de terceiros.
- Diarização (quem falou) é opcional no edge e pode responder 501; não atribua falas sem ela.
- Não reenvie o mesmo arquivo várias vezes: o edge reaproveita transcrições (`transcript_cached`).
- A chave `EDGE_API_KEY` é passada ao script pelo ambiente; nunca a imprima nem a coloque em comandos.

## Verification

- O JSON tem `txt_uri` e `ingest[]` não vazio (quando `ingest` ativo), e
  `mcp__knowledge__knowledge_search(tenant=<tenant>, query="<título>")` encontra a transcrição.

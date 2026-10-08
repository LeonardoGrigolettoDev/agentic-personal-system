# Checklist de revisão (ordem de prioridade)

Use como roteiro, não como formulário: só reporte o que tiver cenário concreto.

## 1. Corretude
- Casos de borda: vazio, nulo/nil, zero, negativo, limites de faixa, Unicode, fuso horário/DST.
- Erros: retornos de erro ignorados, exceções engolidas, `defer`/`finally` que mascaram falhas.
- Lógica: condições invertidas, off-by-one, comparação de floats/dinheiro (use decimal/centavos).
- Estado: mutação compartilhada, cache sem invalidação, idempotência de handlers e jobs.

## 2. Segurança
- Entrada externa sem validação; SQL/shell/template montados por concatenação.
- AuthN/AuthZ: endpoint novo sem checagem; isolamento de tenant (`tenant` sempre filtrado).
- Segredos em código, logs ou mensagens de erro; comparação de tokens sem tempo constante.
- Dependências novas: necessidade real, licença, manutenção.

## 3. Concorrência e recursos
- Corridas (goroutines/threads/async) sem sincronização; deadlocks por ordem de locks.
- Recursos sem fechar (arquivos, conexões, corpos HTTP); timeouts ausentes em I/O de rede.
- Laços sem limite, paginação ausente, carga inteira em memória.

## 4. Contratos e compatibilidade
- APIs públicas, schemas, migrations: mudança retrocompatível? migração reversível?
- Formato de logs/eventos consumido por outros serviços; nomes de env vars.

## 5. Testes
- O teste novo falharia sem a mudança? Cobre o caminho de erro, não só o feliz?
- Testes frágeis: sleeps, ordem de execução, rede real, relógio real.

## 6. Clareza
- Nomes, funções longas, comentários que repetem o código, código morto.
- Só depois de 1–5: preferências de estilo que o lint não cobre.

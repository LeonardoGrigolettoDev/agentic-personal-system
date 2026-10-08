import { test } from "node:test";
import assert from "node:assert/strict";
import { carregaResumo, carregaResumos } from "./resumo.ts";

test("carregaResumo devolve uma Promise com o resumo do cliente VIP", async () => {
  const r = await carregaResumo("p1");
  assert.deepEqual(r, { pedido: "p1", cliente: "Ana Souza", total: 43.03, desconto: 2.27 });
});

test("cliente comum não tem desconto", async () => {
  assert.deepEqual(await carregaResumo("p2"), { pedido: "p2", cliente: "Bruno Lima", total: 50, desconto: 0 });
});

test("erro vira Promise rejeitada", async () => {
  await assert.rejects(carregaResumo("p9"), /pedido p9 não encontrado/);
});

test("carregaResumos mantém a ordem e falha se algum falhar", async () => {
  const rs = await carregaResumos(["p2", "p1"]);
  assert.deepEqual(rs.map((r) => r.pedido), ["p2", "p1"]);
  assert.deepEqual(await carregaResumos([]), []);
  await assert.rejects(carregaResumos(["p1", "p9"]));
});

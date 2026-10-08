import { test } from "node:test";
import assert from "node:assert/strict";
import { produtosSemPreco, valorEstoque, type BuscaPreco } from "./estoque.ts";

const precos: Record<string, number> = { CAFE: 18.9, FILTRO: 7.5, ACUCAR: 5 };
const busca: BuscaPreco = async (sku) => {
  await new Promise((r) => setTimeout(r, 5));
  return precos[sku] ?? Number.NaN;
};

test("valor total do estoque espera todos os preços", async () => {
  const total = await valorEstoque(
    [
      { sku: "CAFE", quantidade: 3 },
      { sku: "FILTRO", quantidade: 4 },
      { sku: "ACUCAR", quantidade: 2 },
    ],
    busca,
  );
  assert.equal(total, 96.7);
});

test("lista produtos sem preço", async () => {
  const faltando = await produtosSemPreco([{ sku: "CAFE", quantidade: 1 }, { sku: "PAO", quantidade: 2 }], busca);
  assert.deepEqual(faltando, ["PAO"]);
});

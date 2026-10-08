import { test } from "node:test";
import assert from "node:assert/strict";
import { slugify, slugUnico } from "./slug.ts";

test("remove acentos e símbolos", () => {
  assert.equal(slugify("Pão de Queijo Mineiro 1kg!"), "pao-de-queijo-mineiro-1kg");
  assert.equal(slugify("  Café   Especial -- Torra Média  "), "cafe-especial-torra-media");
  assert.equal(slugify("Ação & Reação: Edição 2026"), "acao-reacao-edicao-2026");
  assert.equal(slugify("ÇÃÕ ü ñ"), "cao-u-n");
  assert.equal(slugify("!!!"), "");
});

test("respeita o tamanho máximo sem cortar palavra no meio", () => {
  assert.equal(slugify("Kit Churrasco Completo com Faca e Tábua", 20), "kit-churrasco");
  assert.equal(slugify("Supercalifragilisticexpialidocious", 10), "supercalif");
});

test("slug único", () => {
  const existentes = new Set(["cafe", "cafe-2"]);
  assert.equal(slugUnico("Café", existentes), "cafe-3");
  assert.equal(slugUnico("Chá", existentes), "cha");
});

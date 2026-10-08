import { test } from "node:test";
import assert from "node:assert/strict";
import { competencia, proximaCompetencia, vencimento } from "./competencia.ts";

test("competência usa mês de 01 a 12", () => {
  assert.equal(competencia(new Date(2026, 0, 15)), "2026-01");
  assert.equal(competencia(new Date(2026, 11, 31)), "2026-12");
});

test("vencimento recebe mês 1-12 e respeita o último dia do mês", () => {
  const v = vencimento(2026, 10, 10);
  assert.deepEqual([v.getFullYear(), v.getMonth() + 1, v.getDate()], [2026, 10, 10]);
  const fev = vencimento(2026, 2, 31);
  assert.deepEqual([fev.getFullYear(), fev.getMonth() + 1, fev.getDate()], [2026, 2, 28]);
});

test("próxima competência vira o ano", () => {
  assert.equal(proximaCompetencia("2026-09"), "2026-10");
  assert.equal(proximaCompetencia("2026-12"), "2027-01");
});

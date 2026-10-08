// Datas da planilha de finanças pessoais: competência (AAAA-MM) e vencimento das faturas.

/** Competência no formato AAAA-MM (mês 01 a 12). */
export function competencia(data: Date): string {
  return `${data.getFullYear()}-${String(data.getMonth() + 1).padStart(2, "0")}`;
}

/** Data de vencimento; `mes` vai de 1 a 12. Dia maior que o último dia do mês vira o último dia do mês. */
export function vencimento(ano: number, mes: number, dia: number): Date {
  const ultimoDia = new Date(ano, mes, 0).getDate();
  return new Date(ano, mes - 1, Math.min(dia, ultimoDia));
}

/** Competência seguinte à informada ("2026-12" -> "2027-01"). */
export function proximaCompetencia(comp: string): string {
  const [ano, mes] = comp.split("-").map(Number);
  return competencia(new Date(ano, mes, 1));
}

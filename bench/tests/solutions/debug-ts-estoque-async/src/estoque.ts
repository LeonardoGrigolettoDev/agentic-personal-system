// Valor do estoque do app de vendas da Nitro: preço vem de um serviço externo (assíncrono).

export interface Produto {
  sku: string;
  quantidade: number;
}

export type BuscaPreco = (sku: string) => Promise<number>;

export async function valorEstoque(produtos: Produto[], buscaPreco: BuscaPreco): Promise<number> {
  const valores = await Promise.all(produtos.map(async (p) => (await buscaPreco(p.sku)) * p.quantidade));
  const total = valores.reduce((acc, v) => acc + v, 0);
  return Math.round(total * 100) / 100;
}

export async function produtosSemPreco(produtos: Produto[], buscaPreco: BuscaPreco): Promise<string[]> {
  const precos = await Promise.all(produtos.map((p) => buscaPreco(p.sku)));
  return produtos.filter((_, i) => !Number.isFinite(precos[i])).map((p) => p.sku);
}

// Valor do estoque do app de vendas da Nitro: preço vem de um serviço externo (assíncrono).

export interface Produto {
  sku: string;
  quantidade: number;
}

export type BuscaPreco = (sku: string) => Promise<number>;

export async function valorEstoque(produtos: Produto[], buscaPreco: BuscaPreco): Promise<number> {
  let total = 0;
  produtos.forEach(async (p) => {
    const preco = await buscaPreco(p.sku);
    total += preco * p.quantidade;
  });
  return Math.round(total * 100) / 100;
}

export async function produtosSemPreco(produtos: Produto[], buscaPreco: BuscaPreco): Promise<string[]> {
  const faltando: string[] = [];
  for (const p of produtos) {
    buscaPreco(p.sku).then((preco) => {
      if (!Number.isFinite(preco)) faltando.push(p.sku);
    });
  }
  return faltando;
}

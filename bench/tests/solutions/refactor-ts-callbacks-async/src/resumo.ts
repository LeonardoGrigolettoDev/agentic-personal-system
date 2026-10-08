// Resumo de pedido para a tela de atendimento.
import { buscaCliente, buscaPedido } from "./db.ts";

export interface Resumo {
  pedido: string;
  cliente: string;
  total: number;
  desconto: number;
}

const centavos = (v: number) => Math.round(v * 100) / 100;

export async function carregaResumo(id: string): Promise<Resumo> {
  const pedido = await buscaPedido(id);
  const cliente = await buscaCliente(pedido.clienteId);
  const bruto = pedido.itens.reduce((s, i) => s + i.quantidade * i.preco, 0);
  const desconto = cliente.vip ? centavos(bruto * 0.05) : 0;
  return { pedido: pedido.id, cliente: cliente.nome, total: centavos(bruto - desconto), desconto };
}

export async function carregaResumos(ids: string[]): Promise<Resumo[]> {
  return Promise.all(ids.map((id) => carregaResumo(id)));
}

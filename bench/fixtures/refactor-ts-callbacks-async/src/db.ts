// "Banco" em memória do app de pedidos da Nitro. A latência simula a API real.

export interface Pedido {
  id: string;
  clienteId: string;
  itens: { sku: string; quantidade: number; preco: number }[];
}

export interface Cliente {
  id: string;
  nome: string;
  vip: boolean;
}

const pedidos: Record<string, Pedido> = {
  p1: { id: "p1", clienteId: "c1", itens: [{ sku: "CAFE", quantidade: 2, preco: 18.9 }, { sku: "FILTRO", quantidade: 1, preco: 7.5 }] },
  p2: { id: "p2", clienteId: "c2", itens: [{ sku: "ACUCAR", quantidade: 10, preco: 5 }] },
};

const clientes: Record<string, Cliente> = {
  c1: { id: "c1", nome: "Ana Souza", vip: true },
  c2: { id: "c2", nome: "Bruno Lima", vip: false },
};

const espera = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function buscaPedido(id: string): Promise<Pedido> {
  await espera(3);
  const p = pedidos[id];
  if (!p) throw new Error(`pedido ${id} não encontrado`);
  return p;
}

export async function buscaCliente(id: string): Promise<Cliente> {
  await espera(3);
  const c = clientes[id];
  if (!c) throw new Error(`cliente ${id} não encontrado`);
  return c;
}

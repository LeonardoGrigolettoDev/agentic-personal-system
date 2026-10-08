// Resumo de pedido para a tela de atendimento. Escrito no estilo callback antigo; a equipe quer async/await.
import { buscaCliente, buscaPedido, type Cliente, type Pedido } from "./db.ts";

export interface Resumo {
  pedido: string;
  cliente: string;
  total: number;
  desconto: number;
}

type Callback<T> = (err: Error | null, valor?: T) => void;

function carregaPedido(id: string, cb: Callback<Pedido>): void {
  buscaPedido(id).then((p) => cb(null, p), (e) => cb(e));
}

function carregaCliente(id: string, cb: Callback<Cliente>): void {
  buscaCliente(id).then((c) => cb(null, c), (e) => cb(e));
}

export function carregaResumo(id: string, cb: Callback<Resumo>): void {
  carregaPedido(id, (err, pedido) => {
    if (err || !pedido) {
      cb(err ?? new Error("pedido vazio"));
      return;
    }
    carregaCliente(pedido.clienteId, (err2, cliente) => {
      if (err2 || !cliente) {
        cb(err2 ?? new Error("cliente vazio"));
        return;
      }
      const bruto = pedido.itens.reduce((s, i) => s + i.quantidade * i.preco, 0);
      const desconto = cliente.vip ? Math.round(bruto * 0.05 * 100) / 100 : 0;
      cb(null, { pedido: pedido.id, cliente: cliente.nome, total: Math.round((bruto - desconto) * 100) / 100, desconto });
    });
  });
}

export function carregaResumos(ids: string[], cb: Callback<Resumo[]>): void {
  const out: Resumo[] = [];
  let pendentes = ids.length;
  let falhou = false;
  if (pendentes === 0) {
    cb(null, out);
    return;
  }
  ids.forEach((id, i) => {
    carregaResumo(id, (err, r) => {
      if (falhou) return;
      if (err || !r) {
        falhou = true;
        cb(err ?? new Error("resumo vazio"));
        return;
      }
      out[i] = r;
      pendentes -= 1;
      if (pendentes === 0) cb(null, out);
    });
  });
}

-- Mini-ERP (SQLite): produtos, pedidos e itens.
CREATE TABLE produtos (
  id INTEGER PRIMARY KEY,
  nome TEXT NOT NULL,
  categoria TEXT NOT NULL
);
CREATE TABLE pedidos (
  id INTEGER PRIMARY KEY,
  cliente TEXT NOT NULL,
  criado_em TEXT NOT NULL,           -- ISO 8601, horário de Brasília (sem fuso)
  status TEXT NOT NULL CHECK (status IN ('pago', 'cancelado', 'pendente'))
);
CREATE TABLE itens_pedido (
  pedido_id INTEGER NOT NULL REFERENCES pedidos(id),
  produto_id INTEGER NOT NULL REFERENCES produtos(id),
  quantidade INTEGER NOT NULL,
  preco_unitario REAL NOT NULL,
  desconto REAL NOT NULL DEFAULT 0   -- valor em reais descontado do item inteiro
);

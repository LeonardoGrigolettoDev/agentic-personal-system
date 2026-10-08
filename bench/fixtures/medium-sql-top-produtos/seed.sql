INSERT INTO produtos VALUES
  (1, 'Café Especial 500g', 'mercearia'),
  (2, 'Filtro de Papel 103', 'utilidades'),
  (3, 'Açúcar Demerara 1kg', 'mercearia'),
  (4, 'Leite Integral 1L', 'laticínios'),
  (5, 'Pão de Queijo 1kg', 'congelados'),
  (6, 'Moedor Manual', 'utilidades');

INSERT INTO pedidos VALUES
  (100, 'Ana',    '2026-08-31T23:59:00', 'pago'),
  (101, 'Bruno',  '2026-09-01T00:00:00', 'pago'),
  (102, 'Carla',  '2026-09-05T10:15:00', 'pago'),
  (103, 'Diego',  '2026-09-12T18:40:00', 'cancelado'),
  (104, 'Elisa',  '2026-09-18T09:05:00', 'pago'),
  (105, 'Fábio',  '2026-09-22T14:30:00', 'pendente'),
  (106, 'Gabi',   '2026-09-30T23:59:59', 'pago'),
  (107, 'Hugo',   '2026-10-01T00:00:00', 'pago');

INSERT INTO itens_pedido VALUES
  (100, 6, 3, 189.90, 0),
  (101, 1, 4, 42.50, 0),
  (101, 2, 2, 8.90, 0),
  (102, 5, 6, 32.00, 12.00),
  (102, 4, 12, 5.49, 0),
  (103, 6, 2, 189.90, 0),
  (104, 1, 2, 42.50, 5.00),
  (104, 3, 5, 14.90, 0),
  (105, 5, 10, 32.00, 0),
  (106, 4, 24, 5.49, 6.00),
  (106, 2, 3, 8.90, 0),
  (107, 1, 10, 42.50, 0);

SELECT pr.nome AS produto,
       ROUND(SUM(i.quantidade * i.preco_unitario - i.desconto), 2) AS faturamento
FROM itens_pedido i
JOIN pedidos p ON p.id = i.pedido_id
JOIN produtos pr ON pr.id = i.produto_id
WHERE p.status = 'pago'
  AND p.criado_em >= '2026-09-01' AND p.criado_em < '2026-10-01'
GROUP BY pr.id, pr.nome
ORDER BY faturamento DESC
LIMIT 3;

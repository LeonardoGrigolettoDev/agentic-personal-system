// Package pedidos concentra as regras de preço do app de pedidos da Nitro.
package pedidos

// AplicaDesconto devolve o total em centavos depois de aplicar um desconto percentual (0 a 100).
// Regra do financeiro: o valor do desconto é arredondado para o centavo mais próximo (meio centavo arredonda
// para cima) antes de ser subtraído.
func AplicaDesconto(totalCentavos int64, percentual float64) int64 {
	desconto := float64(totalCentavos) * percentual / 100
	return totalCentavos - int64(desconto)
}

// TotalComFrete soma o frete ao total já com desconto.
func TotalComFrete(totalCentavos int64, percentual float64, freteCentavos int64) int64 {
	return AplicaDesconto(totalCentavos, percentual) + freteCentavos
}

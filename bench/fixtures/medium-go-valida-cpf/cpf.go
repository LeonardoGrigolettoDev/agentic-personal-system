// Package documentos valida documentos brasileiros para o cadastro de clientes do mini-ERP.
package documentos

import "errors"

var ErrCPFInvalido = errors.New("CPF inválido")

// NormalizaCPF aceita CPF com ou sem máscara ("529.982.247-25" ou "52998224725"), valida os dígitos
// verificadores e devolve só os 11 dígitos. CPFs com todos os dígitos iguais são inválidos.
func NormalizaCPF(cpf string) (string, error) {
	panic("TODO: implementar")
}

// FormataCPF devolve o CPF (11 dígitos válidos) no formato 000.000.000-00.
func FormataCPF(cpf string) (string, error) {
	panic("TODO: implementar")
}

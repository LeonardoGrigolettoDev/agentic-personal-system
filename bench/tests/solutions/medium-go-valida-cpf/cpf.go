// Package documentos valida documentos brasileiros para o cadastro de clientes do mini-ERP.
package documentos

import (
	"errors"
	"strings"
)

var ErrCPFInvalido = errors.New("CPF inválido")

// NormalizaCPF aceita CPF com ou sem máscara ("529.982.247-25" ou "52998224725"), valida os dígitos
// verificadores e devolve só os 11 dígitos. CPFs com todos os dígitos iguais são inválidos.
func NormalizaCPF(cpf string) (string, error) {
	s := strings.NewReplacer(".", "", "-", "").Replace(strings.TrimSpace(cpf))
	if len(s) != 11 || strings.Trim(s, "0123456789") != "" || strings.Count(s, s[:1]) == 11 {
		return "", ErrCPFInvalido
	}
	for n := 9; n <= 10; n++ {
		soma := 0
		for i := 0; i < n; i++ {
			soma += int(s[i]-'0') * (n + 1 - i)
		}
		dv := soma * 10 % 11 % 10
		if dv != int(s[n]-'0') {
			return "", ErrCPFInvalido
		}
	}
	return s, nil
}

// FormataCPF devolve o CPF (11 dígitos válidos) no formato 000.000.000-00.
func FormataCPF(cpf string) (string, error) {
	s, err := NormalizaCPF(cpf)
	if err != nil {
		return "", err
	}
	return s[:3] + "." + s[3:6] + "." + s[6:9] + "-" + s[9:], nil
}

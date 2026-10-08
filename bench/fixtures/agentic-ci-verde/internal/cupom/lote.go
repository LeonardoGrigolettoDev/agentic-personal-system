package cupom

import "strings"

// Normaliza remove espaços e coloca os códigos em maiúsculas, sem repetir.
func Normaliza(codigos []string) []string {
    vistos := map[string]bool{}
    var out []string
    for _, c := range codigos {
        c = strings.ToUpper(strings.TrimSpace(c))
        if c == "" || vistos[c] { continue }
        vistos[c] = true
        out = append(out, c)
    }
    return out
}

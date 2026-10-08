package documentos

import (
	"errors"
	"testing"
)

func TestNormalizaCPFValidos(t *testing.T) {
	for entrada, want := range map[string]string{
		"529.982.247-25":   "52998224725",
		"52998224725":      "52998224725",
		" 111.444.777-35 ": "11144477735",
		"390.533.447-05":   "39053344705",
	} {
		got, err := NormalizaCPF(entrada)
		if err != nil || got != want {
			t.Errorf("NormalizaCPF(%q) = %q, %v; want %q", entrada, got, err, want)
		}
	}
}

func TestNormalizaCPFInvalidos(t *testing.T) {
	for _, entrada := range []string{"", "529.982.247-24", "111.111.111-11", "5299822472", "529982247255", "529.982.247-2a", "000.000.000-00"} {
		if _, err := NormalizaCPF(entrada); !errors.Is(err, ErrCPFInvalido) {
			t.Errorf("NormalizaCPF(%q) err = %v, want ErrCPFInvalido", entrada, err)
		}
	}
}

func TestFormataCPF(t *testing.T) {
	got, err := FormataCPF("52998224725")
	if err != nil || got != "529.982.247-25" {
		t.Fatalf("FormataCPF = %q, %v", got, err)
	}
	if _, err := FormataCPF("12345678900"); !errors.Is(err, ErrCPFInvalido) {
		t.Fatalf("FormataCPF inválido err = %v", err)
	}
}

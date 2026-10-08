// gastos: total de gastos por categoria de um extrato CSV (separador ';', valores pt-BR).
package main

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "uso: gastos <extrato.csv>")
		os.Exit(2)
	}
	f, err := os.Open(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer f.Close()
	r := csv.NewReader(f)
	r.Comma = ';'
	linhas, err := r.ReadAll()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	totais := map[string]int64{}
	for i, l := range linhas {
		if i == 0 || len(l) < 4 || l[3] == "receita" {
			continue
		}
		v, err := strconv.ParseFloat(strings.ReplaceAll(strings.ReplaceAll(l[2], ".", ""), ",", "."), 64)
		if err != nil {
			fmt.Fprintf(os.Stderr, "linha %d: %v\n", i+1, err)
			os.Exit(1)
		}
		totais[l[3]] -= int64(math.Round(v * 100))
	}
	out := map[string]float64{}
	for k, c := range totais {
		out[k] = float64(c) / 100
	}
	_ = json.NewEncoder(os.Stdout).Encode(out)
}

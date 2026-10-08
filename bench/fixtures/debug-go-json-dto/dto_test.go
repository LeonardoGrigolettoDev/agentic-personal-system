package api

import "testing"

func TestSerializa(t *testing.T) {
	casos := []struct {
		p    Pedido
		want string
	}{
		{Pedido{ID: 7, Cliente: "Ana", ValorTotal: 150.5}, `{"id":7,"cliente":"Ana","valor_total":150.5,"status":"ativo"}`},
		{Pedido{ID: 8, Cliente: "Bruno", Cancelado: true}, `{"id":8,"cliente":"Bruno","valor_total":0,"status":"cancelado"}`},
	}
	for _, c := range casos {
		got, err := Serializa(c.p)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != c.want {
			t.Errorf("Serializa(%+v)\n got %s\nwant %s", c.p, got, c.want)
		}
	}
}

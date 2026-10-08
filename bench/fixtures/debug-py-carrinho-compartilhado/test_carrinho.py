import unittest
from decimal import Decimal

from carrinho import Carrinho, etiquetas


class TestCarrinho(unittest.TestCase):
    def test_carrinhos_independentes(self):
        a = Carrinho("ana")
        a.adiciona("CAFE", Decimal("18.90"), 2)
        b = Carrinho("bruno")
        self.assertEqual(b.itens, [])
        self.assertEqual(b.total(), Decimal("0"))
        self.assertEqual(a.total(), Decimal("37.80"))

    def test_itens_iniciais_nao_sao_alterados(self):
        iniciais = [("FILTRO", Decimal("7.50"), 1)]
        c = Carrinho("carla", iniciais)
        c.adiciona("ACUCAR", Decimal("5.00"))
        self.assertEqual(len(iniciais), 1)
        self.assertEqual(c.total(), Decimal("12.50"))

    def test_etiquetas(self):
        self.assertEqual(etiquetas(1), ["PEDIDO-1"])
        self.assertEqual(etiquetas(2), ["PEDIDO-2"])
        self.assertEqual(etiquetas(3, ["FRAGIL"]), ["FRAGIL", "PEDIDO-3"])


if __name__ == "__main__":
    unittest.main()

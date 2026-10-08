import unittest

from frete import cotacao, prazo_dias


class TestFrete(unittest.TestCase):
    def test_gratis_acima_do_minimo(self):
        self.assertEqual(cotacao(5, 100, 299.9), 0.0)

    def test_padrao(self):
        self.assertEqual(cotacao(2, 10, 100), 21.8)

    def test_minimo(self):
        self.assertEqual(cotacao(0.1, 0, 50), 14.9)

    def test_pesado_longe(self):
        self.assertEqual(cotacao(40, 600, 100), 323.95)
        self.assertEqual(cotacao(40, 600, 100, expresso=True), 437.33)

    def test_invalido(self):
        with self.assertRaises(ValueError):
            cotacao(0, 10, 10)

    def test_prazo(self):
        self.assertEqual(prazo_dias(100), 2)
        self.assertEqual(prazo_dias(900), 4)
        self.assertEqual(prazo_dias(900, expresso=True), 2)
        self.assertEqual(prazo_dias(100, expresso=True), 1)
        self.assertEqual(prazo_dias(10_000), 12)


if __name__ == "__main__":
    unittest.main()

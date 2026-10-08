import unittest
from decimal import Decimal as D

from price import parcela_price, tabela_price, total_pago


class TestPrice(unittest.TestCase):
    def test_parcela(self):
        self.assertEqual(parcela_price(D("1000"), 3, D("0.01")), D("340.02"))
        self.assertEqual(parcela_price(D("100"), 3, D("0")), D("33.33"))

    def test_tabela_tres_meses(self):
        t = tabela_price(D("1000"), 3, D("0.01"))
        self.assertEqual([r["numero"] for r in t], [1, 2, 3])
        self.assertEqual([r["juros"] for r in t], [D("10.00"), D("6.70"), D("3.37")])
        self.assertEqual([r["amortizacao"] for r in t], [D("330.02"), D("333.32"), D("336.66")])
        self.assertEqual([r["saldo"] for r in t], [D("669.98"), D("336.66"), D("0.00")])
        self.assertEqual(t[-1]["parcela"], D("340.03"))
        self.assertEqual(total_pago(D("1000"), 3, D("0.01")), D("1020.07"))

    def test_tabela_longa_fecha(self):
        t = tabela_price(D("2500"), 12, D("0.0199"))
        self.assertEqual(len(t), 12)
        self.assertEqual(sum(r["amortizacao"] for r in t), D("2500"))
        self.assertEqual(t[-1]["saldo"], D("0.00"))
        self.assertTrue(all(r["parcela"] == t[0]["parcela"] for r in t[:-1]))
        self.assertLessEqual(abs(t[-1]["parcela"] - t[0]["parcela"]), D("0.05"))

    def test_sem_juros(self):
        t = tabela_price(D("100"), 3, D("0"))
        self.assertEqual([r["parcela"] for r in t], [D("33.33"), D("33.33"), D("33.34")])

    def test_invalidos(self):
        for args in [(D("0"), 3, D("0.01")), (D("100"), 0, D("0.01")), (D("100"), 3, D("-0.01"))]:
            with self.assertRaises(ValueError):
                tabela_price(*args)


if __name__ == "__main__":
    unittest.main()

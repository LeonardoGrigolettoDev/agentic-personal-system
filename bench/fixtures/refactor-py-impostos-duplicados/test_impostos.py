import unittest
from decimal import Decimal

from impostos import calcula_icms


class TestICMS(unittest.TestCase):
    def test_sp(self):
        r = calcula_icms("sp", Decimal("1000.00"), Decimal("50.00"), Decimal("100.00"))
        self.assertEqual((r["uf"], r["base"], r["icms"], r["fcp"], r["total"]),
                         ("SP", Decimal("950.00"), Decimal("171.00"), Decimal("0"), Decimal("1121.00")))

    def test_rj_com_fcp(self):
        r = calcula_icms("RJ", Decimal("333.33"))
        self.assertEqual((r["icms"], r["fcp"], r["total"]), (Decimal("66.67"), Decimal("6.67"), Decimal("406.67")))
        self.assertEqual(r["aliquota"], Decimal("0.20"))

    def test_mg(self):
        r = calcula_icms("MG", Decimal("100.00"), Decimal("0.05"))
        self.assertEqual((r["base"], r["icms"], r["fcp"]), (Decimal("100.05"), Decimal("18.01"), Decimal("2.00")))

    def test_pr(self):
        r = calcula_icms("PR", Decimal("200.00"))
        self.assertEqual((r["icms"], r["total"]), (Decimal("39.00"), Decimal("239.00")))

    def test_erros(self):
        with self.assertRaises(ValueError):
            calcula_icms("BA", Decimal("1"))
        with self.assertRaises(ValueError):
            calcula_icms("SP", Decimal("10"), desconto=Decimal("20"))


if __name__ == "__main__":
    unittest.main()

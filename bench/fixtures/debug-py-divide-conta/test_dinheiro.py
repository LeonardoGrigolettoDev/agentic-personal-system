import unittest
from decimal import Decimal

from dinheiro import divide_conta, formata_brl


class TestDinheiro(unittest.TestCase):
    def test_divide_exato(self):
        self.assertEqual(divide_conta(Decimal("90.00"), 3), [Decimal("30.00")] * 3)

    def test_divide_com_sobra(self):
        self.assertEqual(divide_conta(Decimal("100.00"), 3), [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")])
        partes = divide_conta(Decimal("10.00"), 6)
        self.assertEqual(sum(partes), Decimal("10.00"))
        self.assertEqual(partes[0], Decimal("1.67"))
        self.assertEqual(partes[-1], Decimal("1.66"))

    def test_formata(self):
        self.assertEqual(formata_brl(Decimal("1234.5")), "R$ 1.234,50")
        self.assertEqual(formata_brl(Decimal("0.1") + Decimal("0.2")), "R$ 0,30")
        self.assertEqual(formata_brl(Decimal("1234567.891")), "R$ 1.234.567,89")


if __name__ == "__main__":
    unittest.main()

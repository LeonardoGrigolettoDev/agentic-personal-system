import unittest

from paginacao import pagina, total_paginas


class TestPaginacao(unittest.TestCase):
    def test_total_paginas(self):
        self.assertEqual(total_paginas(0), 0)
        self.assertEqual(total_paginas(20), 1)
        self.assertEqual(total_paginas(21), 2)
        self.assertEqual(total_paginas(45, tamanho=10), 5)

    def test_primeira_pagina(self):
        itens = list(range(1, 46))
        self.assertEqual(pagina(itens, 1, 10), list(range(1, 11)))

    def test_ultima_pagina_parcial(self):
        itens = list(range(1, 46))
        self.assertEqual(pagina(itens, 5, 10), [41, 42, 43, 44, 45])
        self.assertEqual(pagina(itens, 6, 10), [])

    def test_pagina_invalida(self):
        with self.assertRaises(ValueError):
            pagina([1, 2], 0)


if __name__ == "__main__":
    unittest.main()

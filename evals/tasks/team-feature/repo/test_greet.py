import unittest

from greet import greet


class GreetTest(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("Ann"), "Hello, Ann!")


if __name__ == "__main__":
    unittest.main()

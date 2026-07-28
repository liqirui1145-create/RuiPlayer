import importlib
import unittest


class EqualizerTests(unittest.TestCase):
    def test_flat_preset_is_zeroed(self):
        p2 = importlib.import_module("p2")
        values = p2.get_equalizer_preset_values("平直")
        self.assertEqual(values, [0.0] * len(values))

    def test_rock_preset_has_boost(self):
        p2 = importlib.import_module("p2")
        values = p2.get_equalizer_preset_values("摇滚")
        self.assertGreater(values[0], 0)
        self.assertGreater(values[-1], 0)


if __name__ == "__main__":
    unittest.main()

import unittest

from core.cooldown import Cooldown


class CooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.cooldown = Cooldown(60, clock=lambda: self.now[0])

    def test_a_key_is_answered_once_until_its_claim_runs_out(self) -> None:
        self.assertTrue(self.cooldown.claim(("g1", "账号 x")))
        self.now[0] += 59
        self.assertFalse(self.cooldown.claim(("g1", "账号 x")))
        self.now[0] += 1
        self.assertTrue(self.cooldown.claim(("g1", "账号 x")))

    def test_keys_are_independent(self) -> None:
        self.assertTrue(self.cooldown.claim(("g1", "账号 x")))
        self.assertTrue(self.cooldown.claim(("g2", "账号 x")))
        self.assertTrue(self.cooldown.claim(("g1", "趋势 x")))

    def test_a_released_key_is_answered_again_at_once(self) -> None:
        self.cooldown.claim("k")
        self.cooldown.release("k")
        self.assertTrue(self.cooldown.claim("k"))
        # Releasing what nobody holds is harmless.
        self.cooldown.release("never")


if __name__ == "__main__":
    unittest.main()

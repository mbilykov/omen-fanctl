"""Contract tests for shared test doubles."""

import inspect
import unittest

from omen_fanctl.hardware import HpFanHwmon

from tests.helpers import FakeFan


class FakeFanContractTests(unittest.TestCase):
    def test_controller_stub_matches_hp_fan_public_method_signatures(self):
        for name in (
            "status",
            "set_manual",
            "update_manual",
            "set_maximum",
            "restore_auto",
        ):
            with self.subTest(method=name):
                real = inspect.signature(getattr(HpFanHwmon, name))
                fake = inspect.signature(getattr(FakeFan, name))
                real_parameters = tuple(
                    (parameter.name, parameter.kind, parameter.default)
                    for parameter in real.parameters.values()
                )
                fake_parameters = tuple(
                    (parameter.name, parameter.kind, parameter.default)
                    for parameter in fake.parameters.values()
                )
                self.assertEqual(fake_parameters, real_parameters)


if __name__ == "__main__":
    unittest.main()

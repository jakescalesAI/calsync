"""Windows has no fcntl, so these load the engine as Windows would and stand in for msvcrt and winreg."""
import importlib.util, os, sys, tempfile, types, unittest
from unittest import mock


def load_as_windows(msvcrt):
    spec = importlib.util.spec_from_file_location("calsync_win", os.path.join(os.path.dirname(__file__) or ".", "calsync.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": msvcrt}):
        spec.loader.exec_module(module)
    return module


class FakeMsvcrt(types.ModuleType):
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        super().__init__("msvcrt")
        self.held = set()

    def locking(self, fd, mode, size):
        name = os.path.realpath(f"/proc/self/fd/{fd}")
        if mode == self.LK_UNLCK:
            self.held.discard(name)
        elif name in self.held:
            raise OSError(13, "locked")
        else:
            self.held.add(name)


class WindowsTest(unittest.TestCase):
    def setUp(self):
        self.msvcrt = FakeMsvcrt()
        self.engine = load_as_windows(self.msvcrt)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine.HOME = self.tmp.name
        self.engine.SECRETS = os.path.join(self.tmp.name, "secrets")

    def test_engine_loads_without_fcntl(self):
        self.assertIsNone(self.engine.fcntl)

    def test_a_held_lock_refuses_a_second_run_then_frees(self):
        with self.engine.file_lock("run.lock", blocking=False) as first:
            self.assertTrue(first)
            with self.engine.file_lock("run.lock", blocking=False) as second:
                self.assertFalse(second)
        with self.engine.file_lock("run.lock", blocking=False) as again:
            self.assertTrue(again)

    def test_zone_comes_from_the_registry(self):
        key = mock.MagicMock()
        key.__enter__.return_value = key
        reg = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, OpenKey=lambda *a: key,
                                    QueryValueEx=lambda k, n: ("Eastern Standard Time", 1))
        with mock.patch.dict(sys.modules, {"winreg": reg}):
            self.assertEqual(self.engine.windows_zone(), "America/New_York")
        reg.QueryValueEx = lambda k, n: ("Somewhere Else Time", 1)
        with mock.patch.dict(sys.modules, {"winreg": reg}):
            self.assertEqual(self.engine.windows_zone(), "")

    def test_no_registry_means_no_zone(self):
        with mock.patch.dict(sys.modules, {"winreg": None}):
            self.assertEqual(self.engine.windows_zone(), "")


if __name__ == "__main__":
    unittest.main()

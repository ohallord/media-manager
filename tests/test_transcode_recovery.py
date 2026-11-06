import unittest

from media_manager.transcode.recovery import (
    CLEANUP_RETRY, COMPLETE, FINISH_SWAP, MANUAL, ROLLBACK, plan_recovery,
)


def ex(source=False, backup=False, temp=False, output=False):
    return {"source": source, "backup": backup, "temp": temp, "output": output}


class TestPlanRecovery(unittest.TestCase):
    def test_pre_swap_phases(self):
        for phase in ("queued", "preparing", "encoding", "verifying", "installing"):
            with self.subTest(phase=phase):
                self.assertEqual(plan_recovery(phase, ex(source=True, temp=True), False, None), CLEANUP_RETRY)
                self.assertEqual(plan_recovery(phase, ex(source=False), False, None), MANUAL)
                self.assertEqual(plan_recovery(phase, ex(source=True, backup=True), False, None), MANUAL)

    def test_swap_different_paths(self):
        # crash before the first rename
        self.assertEqual(plan_recovery("swapping", ex(source=True, temp=True), False, True), CLEANUP_RETRY)
        self.assertEqual(plan_recovery("swapping", ex(temp=True), False, True), MANUAL)
        # between the renames
        self.assertEqual(plan_recovery("swapping", ex(backup=True, temp=True), False, True), FINISH_SWAP)
        self.assertEqual(plan_recovery("swapping", ex(backup=True, temp=True), False, False), ROLLBACK)
        self.assertEqual(plan_recovery("swapping", ex(backup=True, temp=True), False, None), ROLLBACK)
        # after both renames
        self.assertEqual(plan_recovery("swapped", ex(backup=True, output=True), False, True), COMPLETE)
        self.assertEqual(plan_recovery("swapped", ex(backup=True, output=True), False, False), ROLLBACK)
        # new file vanished / impossible states
        self.assertEqual(plan_recovery("swapped", ex(backup=True), False, None), ROLLBACK)
        self.assertEqual(plan_recovery("swapped", ex(backup=True, temp=True, output=True), False, True), MANUAL)

    def test_swap_same_path_mkv_source(self):
        # source and output are the same path: 'output' means that path exists
        self.assertEqual(plan_recovery("swapping", ex(output=True, temp=True), True, True), CLEANUP_RETRY)
        self.assertEqual(plan_recovery("swapping", ex(backup=True, temp=True), True, True), FINISH_SWAP)
        self.assertEqual(plan_recovery("swapped", ex(backup=True, output=True), True, True), COMPLETE)
        self.assertEqual(plan_recovery("swapped", ex(backup=True, output=True), True, False), ROLLBACK)


if __name__ == "__main__":
    unittest.main()

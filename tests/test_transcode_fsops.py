import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from media_manager.transcode import fsops


class TestFsops(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mm_fsops_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name, data=b"hello"):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_volume_ok(self):
        self.assertFalse(fsops.volume_ok(None))
        self.assertFalse(fsops.volume_ok(self.tmp))  # empty = dropped mount
        self._file("x")
        self.assertTrue(fsops.volume_ok(self.tmp))
        with patch("os.listdir", side_effect=PermissionError):
            self.assertFalse(fsops.volume_ok(self.tmp))

    def test_storage_root_and_is_inside(self):
        roots = ["/mnt/Movies", "/media/Movies/", ""]
        self.assertEqual(fsops.storage_root("/media/Movies/A/a.mkv", roots), "/media/Movies")
        self.assertEqual(fsops.storage_root("/mnt/Movies/A/a.mkv", roots), "/mnt/Movies")
        self.assertEqual(fsops.storage_root("/other/A/a.mkv", roots), "/other/A")
        self.assertTrue(fsops.is_inside(os.path.join(self.tmp, "a"), [self.tmp]))
        self.assertTrue(fsops.is_inside(self.tmp, [self.tmp + "/"]))
        self.assertFalse(fsops.is_inside(self.tmp + "-other", [self.tmp, ""]))

    def test_filesystem_type_from_mounts(self):
        mounts = self._file("mounts", b"/dev/sda1 / ext4 rw 0 0\n//nas/tv /mnt/Tv\\040Shows cifs rw 0 0\nbad\n")
        self.assertEqual(fsops.filesystem_type("/mnt/Tv Shows/x.mkv", mounts), "cifs")
        self.assertTrue(fsops.is_network_path("/mnt/Tv Shows/x.mkv", mounts))
        self.assertFalse(fsops.is_network_path("/home/x", mounts))
        self.assertEqual(fsops.filesystem_type("/x", os.path.join(self.tmp, "nope")), "")

    def test_paths(self):
        self.assertEqual(fsops.output_path_for("/a/b/Movie (2000).mp4"), "/a/b/Movie (2000).mkv")
        self.assertEqual(fsops.temp_path_for("/a/b/Movie.mkv", "abcdef123456"), "/a/b/.Movie.abcdef12.mm-partial")
        self.assertEqual(fsops.backup_path_for("/a/b/Movie.mkv"), "/a/b/.Movie.mkv.mm-orig")

    def test_find_backup_follows_filebot_rename(self):
        legacy = os.path.join(self.tmp, "Movie (2000).avi.mm-orig")
        renamed = os.path.join(self.tmp, "Movie (2000).mm-orig")
        hidden = fsops.backup_path_for(os.path.join(self.tmp, "Movie (2000).mkv"))
        self.assertEqual(fsops.backup_candidates(legacy), [legacy, renamed])
        self.assertEqual(fsops.backup_candidates(hidden), [hidden])
        self.assertEqual(fsops.backup_candidates("/a/odd.bak"), ["/a/odd.bak"])
        self.assertIsNone(fsops.find_backup(legacy))
        self.assertIsNone(fsops.find_backup(None))
        self._file("Movie (2000).mm-orig", b"x")
        self.assertEqual(fsops.find_backup(legacy), renamed)
        self._file("Movie (2000).avi.mm-orig", b"x")
        self.assertEqual(fsops.find_backup(legacy), legacy)

    def test_copy_hash_and_helpers(self):
        src = self._file("src.bin", os.urandom(fsops.CHUNK + 123))
        dst = os.path.join(self.tmp, "dst.bin")
        size, digest = fsops.copy_with_hash(src, dst)
        self.assertEqual(size, fsops.CHUNK + 123)
        self.assertEqual(digest, fsops.hash_file(dst))
        self.assertEqual(digest, fsops.hash_file(src))
        self.assertTrue(fsops.same_filesystem(src, dst))
        self.assertFalse(fsops.same_filesystem(src, "/no/such"))
        self.assertGreater(fsops.free_bytes(self.tmp), 0)
        self.assertTrue(fsops.can_write_dir(self.tmp))
        fsops.remove_quietly(dst)
        fsops.remove_quietly(dst)
        fsops.remove_quietly(None)
        self.assertFalse(os.path.exists(dst))

    def test_ownership_and_dir_fsync_tolerate_errors(self):
        path = self._file("o")
        with patch("os.chown", side_effect=PermissionError), patch("os.chmod", side_effect=PermissionError):
            fsops.apply_ownership(path, 1000, 1000, 0o664)
        fsops.apply_ownership(path, os.getuid(), os.getgid(), 0o640)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        fsops.fsync_dir("/no/such/dir")
        with patch("os.fsync", side_effect=OSError):
            fsops.fsync_dir(self.tmp)


if __name__ == "__main__":
    unittest.main()

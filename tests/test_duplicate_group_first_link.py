"""A brand-new duplicate group must link its items on the first run.

update_duplicate_groups() assigned item.duplicate_group_id = dup_group.id
before the new group was flushed, so the id was still None: the group row
existed but no item pointed at it until the next run. The Duplicates page
showed "No Duplicate Files Detected" for freshly found duplicates.
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime

from media_manager.database import SyncSessionLocal
from media_manager.models import DuplicateGroup, MediaItem
from media_manager.quality import update_duplicate_groups

TITLE = "_UT_FirstLink_Movie_"


def _item(path, volume, score):
    return MediaItem(path=path, storage_volume=volume, title=TITLE, media_type="movie", resolution="1080p",
                     height=1080, width=1920, video_codec="h264", audio_codec="AAC", audio_channels=2,
                     bitrate=1, size_bytes=1, quality_score=score, file_created_at=datetime.utcnow())


class TestDuplicateGroupFirstLink(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mm_dup_first_")
        self.paths = []
        for name, score in (("a", 900), ("b", 400)):
            vol = os.path.join(self.root, name)
            os.makedirs(vol)
            path = os.path.join(vol, f"{TITLE}.mkv")
            open(path, "wb").close()
            self.paths.append((path, vol, score))
        s = SyncSessionLocal()
        s.add_all([_item(p, v, sc) for p, v, sc in self.paths])
        s.commit()
        s.close()

    def tearDown(self):
        s = SyncSessionLocal()
        s.query(MediaItem).filter(MediaItem.title == TITLE).delete()
        s.query(DuplicateGroup).filter(DuplicateGroup.media_title == TITLE).delete()
        s.commit()
        s.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_items_linked_on_first_run(self):
        update_duplicate_groups()
        s = SyncSessionLocal()
        try:
            group = s.query(DuplicateGroup).filter_by(media_title=TITLE).one()
            linked = s.query(MediaItem).filter(MediaItem.title == TITLE).all()
            self.assertEqual({i.duplicate_group_id for i in linked}, {group.id})
            best = next(i for i in linked if i.quality_score == 900)
            self.assertEqual(group.best_media_id, best.id)
            self.assertEqual(group.total_count, 2)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()

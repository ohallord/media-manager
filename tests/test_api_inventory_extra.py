import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.web.api.auth import get_current_user, get_admin_user
from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem, DuplicateGroup
import media_manager.web.api.inventory as inventory_module


SEEDED_PATHS = [
    "/media/Movies/AlphaInvExtraTest (2020)/AlphaInvExtraTest (2020).mkv",
    "/media/Movies/The BetaInvExtraTest (2021)/The BetaInvExtraTest (2021).mkv",
    "/media/TvShows/IotaInvExtraShowTest/Season 1/1x01 - Pilot.mkv",
]


class TestInventoryItemsFiltering(unittest.TestCase):
    """Exercises list_inventory_items' filtering/letter-index branches that
    the baseline tests (plain GET with no query params) never touch."""

    @classmethod
    def setUpClass(cls):
        session = SyncSessionLocal()
        try:
            cls.items = [
                MediaItem(
                    path=SEEDED_PATHS[0], storage_volume="/media/Movies",
                    title="AlphaInvExtraTest", media_type="movie",
                    resolution="4K", height=2160, width=3840,
                    video_codec="hevc", audio_codec="atmos", audio_channels=8,
                    bitrate=20_000_000, size_bytes=1, quality_score=1000,
                    file_created_at=datetime.utcnow(),
                ),
                MediaItem(
                    path=SEEDED_PATHS[1], storage_volume="/media/Movies",
                    title="The BetaInvExtraTest", media_type="movie",
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="aac", audio_channels=2,
                    bitrate=5_000_000, size_bytes=1, quality_score=500,
                    file_created_at=datetime.utcnow(),
                ),
                MediaItem(
                    path=SEEDED_PATHS[2], storage_volume="/media/TvShows",
                    title="IotaInvExtraShowTest", media_type="tv", season=1, episode=1,
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="ac3", audio_channels=6,
                    bitrate=5_000_000, size_bytes=1, quality_score=500,
                    file_created_at=datetime.utcnow(),
                ),
            ]
            session.add_all(cls.items)
            session.commit()
        finally:
            session.close()

    @classmethod
    def tearDownClass(cls):
        session = SyncSessionLocal()
        try:
            session.query(MediaItem).filter(MediaItem.path.in_(SEEDED_PATHS)).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def setUp(self):
        app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_admin_user] = lambda: {"username": "admin", "role": "admin"}
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_search_query_filter(self):
        resp = self.client.get("/api/v1/inventory/items", params={"q": "AlphaInvExtraTest"})
        self.assertEqual(resp.status_code, 200)
        titles = [i["title"] for i in resp.json()["items"]]
        self.assertIn("AlphaInvExtraTest", titles)

    def test_media_type_filter(self):
        resp = self.client.get("/api/v1/inventory/items", params={"media_type": "tv"})
        self.assertEqual(resp.status_code, 200)
        for item in resp.json()["items"]:
            self.assertEqual(item["media_type"], "tv")

    def test_resolution_filter(self):
        resp = self.client.get("/api/v1/inventory/items", params={"resolution": "4K"})
        self.assertEqual(resp.status_code, 200)
        titles = [i["title"] for i in resp.json()["items"]]
        self.assertIn("AlphaInvExtraTest", titles)

    def test_audio_codec_filter(self):
        resp = self.client.get("/api/v1/inventory/items", params={"audio_codec": "atmos"})
        self.assertEqual(resp.status_code, 200)
        titles = [i["title"] for i in resp.json()["items"]]
        self.assertIn("AlphaInvExtraTest", titles)

    def test_letter_filter_non_tv_alpha(self):
        resp = self.client.get("/api/v1/inventory/items", params={"letter": "A"})
        self.assertEqual(resp.status_code, 200)
        titles = [i["title"] for i in resp.json()["items"]]
        self.assertIn("AlphaInvExtraTest", titles)

    def test_letter_filter_non_tv_strips_the_prefix(self):
        resp = self.client.get("/api/v1/inventory/items", params={"letter": "B"})
        self.assertEqual(resp.status_code, 200)
        titles = [i["title"] for i in resp.json()["items"]]
        self.assertIn("The BetaInvExtraTest", titles)

    def test_letter_filter_non_tv_hash_digit(self):
        resp = self.client.get("/api/v1/inventory/items", params={"letter": "#"})
        self.assertEqual(resp.status_code, 200)

    def test_letter_filter_tv_matches_show_name(self):
        resp = self.client.get("/api/v1/inventory/items", params={"letter": "I", "media_type": "tv"})
        self.assertEqual(resp.status_code, 200)
        titles = [i["title"] for i in resp.json()["items"]]
        self.assertIn("IotaInvExtraShowTest", titles)

    def test_letter_filter_tv_hash_digit_no_match(self):
        resp = self.client.get("/api/v1/inventory/items", params={"letter": "#", "media_type": "tv"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["letter"], "#")

    def test_pagination_params(self):
        resp = self.client.get("/api/v1/inventory/items", params={"limit": 1, "offset": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(resp.json()["items"]), 1)


class TestDeleteInventoryItem(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_admin_user] = lambda: {"username": "admin", "role": "admin"}
        self.client = TestClient(app)
        inventory_module.clear_duplicates_cache()

    def tearDown(self):
        app.dependency_overrides.clear()
        session = SyncSessionLocal()
        try:
            session.query(MediaItem).filter(MediaItem.title == "_InvExtra_DeleteMe_").delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def _seed_one(self, path="/tmp/_inv_extra_delete_test_/does_not_exist_on_disk.mkv"):
        session = SyncSessionLocal()
        try:
            item = MediaItem(
                path=path, storage_volume="/tmp", title="_InvExtra_DeleteMe_", media_type="movie",
                resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="aac", audio_channels=2,
                bitrate=1_000_000, size_bytes=1, quality_score=100,
                file_created_at=datetime.utcnow(),
            )
            session.add(item)
            session.commit()
            return item.id
        finally:
            session.close()

    def test_delete_not_found_returns_404(self):
        resp = self.client.delete("/api/v1/inventory/items/nonexistent-id")
        self.assertEqual(resp.status_code, 404)

    def test_delete_missing_file_on_disk_still_deletes_db_row(self):
        item_id = self._seed_one()
        with patch("media_manager.quality.volume_is_healthy", return_value=True):
            resp = self.client.delete(f"/api/v1/inventory/items/{item_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")

    def test_delete_refuses_when_volume_unhealthy(self):
        item_id = self._seed_one()
        with patch("media_manager.quality.volume_is_healthy", return_value=False):
            resp = self.client.delete(f"/api/v1/inventory/items/{item_id}")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("unmounted", resp.json()["detail"])

    def test_delete_permission_error_returns_403(self):
        item_id = self._seed_one(path="/tmp/_inv_extra_delete_test_/protected.mkv")
        with patch("media_manager.quality.volume_is_healthy", return_value=True), \
             patch("media_manager.web.api.inventory.os.path.exists", return_value=True), \
             patch("media_manager.web.api.inventory.os.remove", side_effect=PermissionError("denied")):
            resp = self.client.delete(f"/api/v1/inventory/items/{item_id}")
        self.assertEqual(resp.status_code, 403)

    def test_delete_generic_os_error_returns_500(self):
        item_id = self._seed_one(path="/tmp/_inv_extra_delete_test_/errors.mkv")
        with patch("media_manager.quality.volume_is_healthy", return_value=True), \
             patch("media_manager.web.api.inventory.os.path.exists", return_value=True), \
             patch("media_manager.web.api.inventory.os.remove", side_effect=OSError("disk fell off")):
            resp = self.client.delete(f"/api/v1/inventory/items/{item_id}")
        self.assertEqual(resp.status_code, 500)


class TestDuplicatesCachingAndDeleteWorse(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_admin_user] = lambda: {"username": "admin", "role": "admin"}
        self.client = TestClient(app)
        inventory_module.clear_duplicates_cache()
        self.group_id = None
        self.item_ids = []

    def tearDown(self):
        app.dependency_overrides.clear()
        inventory_module.clear_duplicates_cache()
        session = SyncSessionLocal()
        try:
            if self.item_ids:
                session.query(MediaItem).filter(MediaItem.id.in_(self.item_ids)).delete(synchronize_session=False)
            if self.group_id:
                session.query(DuplicateGroup).filter_by(id=self.group_id).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def _seed_duplicate_group(self):
        session = SyncSessionLocal()
        try:
            best = MediaItem(
                path="/tmp/_inv_extra_dupe_/best.mkv", storage_volume="/tmp",
                title="_InvExtra_Dupe_", media_type="movie",
                resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="aac", audio_channels=2,
                bitrate=5_000_000, size_bytes=2_000_000_000, quality_score=500,
                file_created_at=datetime.utcnow(),
            )
            worse = MediaItem(
                path="/tmp/_inv_extra_dupe_/worse.mkv", storage_volume="/tmp",
                title="_InvExtra_Dupe_", media_type="movie",
                resolution="720p", height=720, width=1280,
                video_codec="h264", audio_codec="aac", audio_channels=2,
                bitrate=2_000_000, size_bytes=500_000_000, quality_score=100,
                file_created_at=datetime.utcnow(),
            )
            session.add_all([best, worse])
            session.commit()
            self.item_ids = [best.id, worse.id]

            group = DuplicateGroup(
                group_key="movie:_invextra_dupe_", media_title="_InvExtra_Dupe_",
                season_episode="Movie", best_media_id=best.id, total_count=2,
            )
            session.add(group)
            session.commit()
            self.group_id = group.id

            best.duplicate_group_id = group.id
            worse.duplicate_group_id = group.id
            session.add_all([best, worse])
            session.commit()
        finally:
            session.close()

    def test_duplicates_endpoint_lists_group_and_caches(self):
        self._seed_duplicate_group()

        first = self.client.get("/api/v1/inventory/duplicates")
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["cached"])
        titles = [g["media_title"] for g in first.json()["groups"]]
        self.assertIn("_InvExtra_Dupe_", titles)

        second = self.client.get("/api/v1/inventory/duplicates")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["cached"])

        third = self.client.get("/api/v1/inventory/duplicates", params={"force_refresh": True})
        self.assertEqual(third.status_code, 200)
        self.assertFalse(third.json()["cached"])

    def test_delete_worse_duplicates_for_specific_group(self):
        self._seed_duplicate_group()
        with patch("media_manager.quality.volume_is_healthy", return_value=True), \
             patch("media_manager.web.api.inventory.os.path.exists", return_value=False):
            resp = self.client.post("/api/v1/inventory/duplicates/delete-worse", params={"group_id": self.group_id})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["deleted_count"], 1)
        self.assertEqual(data["freed_bytes"], 500_000_000)

    def test_delete_worse_duplicates_all_with_none_to_delete(self):
        resp = self.client.post("/api/v1/inventory/duplicates/delete-worse", params={"group_id": "all"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted_count"], 0)

    def test_delete_worse_duplicates_permission_denied_counted(self):
        self._seed_duplicate_group()
        with patch("media_manager.quality.volume_is_healthy", return_value=True), \
             patch("media_manager.web.api.inventory.os.path.exists", return_value=True), \
             patch("media_manager.web.api.inventory.os.remove", side_effect=PermissionError("Permission denied")):
            resp = self.client.post("/api/v1/inventory/duplicates/delete-worse", params={"group_id": self.group_id})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["deleted_count"], 0)
        self.assertEqual(data["permission_denied_count"], 1)

    def test_delete_worse_refuses_when_volume_unhealthy(self):
        self._seed_duplicate_group()
        with patch("media_manager.quality.volume_is_healthy", return_value=False):
            resp = self.client.post("/api/v1/inventory/duplicates/delete-worse", params={"group_id": self.group_id})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("unmounted", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()

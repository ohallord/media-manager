import unittest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select
from media_manager.database import AsyncSessionLocal
from media_manager.models import MetadataCache
from media_manager.web.api.integrations import (
    IN_MEMORY_ARTWORK_CACHE,
    IN_MEMORY_METADATA_CACHE,
    get_media_artwork,
    get_media_metadata,
    get_combined_calendar,
)
from media_manager.integrations.sonarr import SonarrClient

class TestMultiTierCaching(unittest.TestCase):

    def test_artwork_cache_memory_tier(self):
        async def run():
            async with AsyncSessionLocal() as db:
                title = "Test Cache Movie"
                cache_key = f"movie:{title.lower()}"
                
                # Populate memory cache directly
                mock_artwork = {"poster": "/path/to/poster.jpg", "background": "/path/to/bg.jpg", "logo": None, "banner": None, "source": "unit_test"}
                IN_MEMORY_ARTWORK_CACHE[cache_key] = mock_artwork

                res = await get_media_artwork(title=title, type="movie", db=db)
                self.assertTrue(res['cached'])
                self.assertEqual(res['cache_tier'], 'memory')
                self.assertEqual(res['artwork']['source'], 'unit_test')

                # Cleanup
                del IN_MEMORY_ARTWORK_CACHE[cache_key]

        asyncio.run(run())

    def test_metadata_cache_memory_tier(self):
        async def run():
            async with AsyncSessionLocal() as db:
                title = "Test Cache Show"
                cache_key = f"tv:{title.lower()}"

                # Populate memory cache
                mock_meta = {"title": title, "rating": 9.5, "genres": ["Drama"], "overview": "Test overview"}
                IN_MEMORY_METADATA_CACHE[cache_key] = mock_meta

                res = await get_media_metadata(title=title, type="tv", db=db)
                self.assertTrue(res['cached'])
                self.assertEqual(res['cache_tier'], 'memory')
                self.assertEqual(res['metadata']['rating'], 9.5)

                # Cleanup
                del IN_MEMORY_METADATA_CACHE[cache_key]

        asyncio.run(run())

    def test_metadata_db_persistence_json_roundtrip(self):
        """
        Regression test: get_media_metadata() always attempts to persist its
        result to the MetadataCache table (genres_json/cast_json are built via
        json.dumps), even when no external provider match is found. A missing
        `import json` in media_manager/web/api/integrations.py previously made
        that persistence step raise a silent NameError (swallowed by a bare
        `except Exception`), so nothing was ever written to the database.
        This test proves the DB row is actually created and its JSON columns
        round-trip correctly.
        """
        async def run():
            async with AsyncSessionLocal() as db:
                title = "Zzz Regression Test Movie For Metadata Cache"
                cache_key = f"movie:{title.lower()}"

                # Ensure a clean slate across repeated runs.
                IN_MEMORY_METADATA_CACHE.pop(cache_key, None)
                existing = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
                existing_row = existing.scalar_one_or_none()
                if existing_row:
                    await db.delete(existing_row)
                    await db.commit()

                res = await get_media_metadata(title=title, type="movie", db=db)
                self.assertFalse(res['cached'])

                check = await db.execute(select(MetadataCache).where(MetadataCache.cache_key == cache_key))
                saved = check.scalar_one_or_none()
                self.assertIsNotNone(saved, "metadata cache row was not persisted to the database")
                # genres_json/cast_json must be valid, parseable JSON strings.
                self.assertEqual(json.loads(saved.genres_json), res['metadata']['genres'])
                self.assertEqual(json.loads(saved.cast_json), res['metadata']['cast'])

                # Cleanup
                IN_MEMORY_METADATA_CACHE.pop(cache_key, None)
                await db.delete(saved)
                await db.commit()

        asyncio.run(run())

    def test_calendar_survives_provider_errors(self):
        """
        Regression test: get_combined_calendar() wraps each provider call in
        try/except and logs failures via a module-level `logger`. That name
        was never defined in media_manager/web/api/integrations.py, so any
        Radarr/Sonarr API failure raised a NameError from inside the except
        block itself instead of being handled gracefully, turning a partial
        provider outage into a hard 500 for the whole calendar endpoint.
        This uses mocked config/session objects so it never touches the real
        database or network.
        """
        async def run():
            fake_radarr_cfg = MagicMock(enabled=False)
            fake_sonarr_cfg = MagicMock(enabled=True, api_url="http://fake-sonarr.invalid", api_key_encrypted="fake-key")
            fake_tmdb_cfg = MagicMock(enabled=False)

            radarr_result = MagicMock()
            radarr_result.scalar_one_or_none.return_value = fake_radarr_cfg
            sonarr_result = MagicMock()
            sonarr_result.scalar_one_or_none.return_value = fake_sonarr_cfg
            tmdb_result = MagicMock()
            tmdb_result.scalar_one_or_none.return_value = fake_tmdb_cfg

            async def fake_execute(statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "integration_configs" in compiled and "'radarr'" in compiled:
                    return radarr_result
                if "integration_configs" in compiled and "'sonarr'" in compiled:
                    return sonarr_result
                if "integration_configs" in compiled and "'tmdb'" in compiled:
                    return tmdb_result
                if "app_log_events" in compiled:
                    log_res = MagicMock()
                    log_res.scalars.return_value.all.return_value = []
                    return log_res
                return MagicMock()

            mock_db = AsyncMock()
            mock_db.execute = fake_execute

            with patch.object(SonarrClient, "get_series", side_effect=RuntimeError("Sonarr unreachable")):
                events = await get_combined_calendar(db=mock_db)

            # Should degrade gracefully to an empty list, not raise.
            self.assertEqual(events, [])

        asyncio.run(run())

if __name__ == '__main__':
    unittest.main()

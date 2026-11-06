"""
H.265 migration logic shared by the web app and the host transcode worker.

Modules here are pure (no DB, no HTTP) so the worker can import them
without the app's database stack, and so they're cheap to unit-test.
"""

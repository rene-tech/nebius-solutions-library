"""A terminal status must not freeze polling before the result is published."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from run_rest import Campaign


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_before_publication_is_polled_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = Campaign.__new__(Campaign)
            campaign.args = SimpleNamespace(timeout=10, requests=1)
            job = {"out": root, "receipt": {"operation_id": "op", "state": "running"}}
            campaign.jobs = {"cohort-1/case": job}
            campaign.drain = AsyncMock()
            async def collect(*args):
                if job["receipt"].get("result_published"):
                    job["receipt"]["state"] = "verified"
                    job["receipt"]["benchmark_complete"] = True
            campaign.collect = collect
            polls = []
            async def get(path, **kwargs):
                if path.endswith("/events"):
                    body = {"data": []}
                else:
                    polls.append(path)
                    body = {"operation": {"status": "succeeded"},
                            "batch": {"result_published": len(polls) >= 2}}
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
            http = SimpleNamespace(get=get)
            clock = SimpleNamespace(monotonic=iter([0, 0, 0, 11]).__next__)
            with patch("run_rest.asyncio.sleep", new=AsyncMock()), \
                 patch("run_rest.time", new=clock):
                self.assertTrue(await campaign.cohort(http, 1))
            self.assertEqual(len(polls), 2)
            self.assertEqual(job["receipt"]["state"], "verified")


if __name__ == "__main__":
    unittest.main()

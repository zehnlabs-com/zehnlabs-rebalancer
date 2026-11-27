from __future__ import annotations

import unittest

from app.allocations import AllocationsClient, AllocationsUnavailable


class FakeResponse:
    status = 200

    def __init__(self, allocation):
        self.allocation = allocation

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def json(self):
        return {
            "status": "success",
            "data": {
                "allocations": [
                    {"ticker": "SPY", "allocation": self.allocation}
                ]
            },
        }


class FakeSession:
    def __init__(self, allocation):
        self.allocation = allocation

    def get(self, *args, **kwargs):
        return FakeResponse(self.allocation)


class AllocationsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_weight_is_parsed(self):
        client = AllocationsClient(
            "https://example.test/strategies",
            "synthetic-key",
            1.0,
        )
        client._session = FakeSession("0.5")

        allocations = await client.fetch("strategy-one")

        self.assertEqual(allocations[0].symbol, "SPY")
        self.assertEqual(allocations[0].weight, 0.5)

    async def test_malformed_weight_is_rejected_at_api_boundary(self):
        for allocation in (True, "not-a-number", {"unexpected": "value"}):
            with self.subTest(allocation=allocation):
                client = AllocationsClient(
                    "https://example.test/strategies",
                    "synthetic-key",
                    1.0,
                )
                client._session = FakeSession(allocation)

                with self.assertRaisesRegex(
                    AllocationsUnavailable,
                    r"invalid response",
                ):
                    await client.fetch("strategy-one")


if __name__ == "__main__":
    unittest.main()

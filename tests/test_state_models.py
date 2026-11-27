from __future__ import annotations

import hashlib
import unittest

from pydantic import ValidationError

from app.results import WorkAction
from app.state.models import (
    RetryDecision,
    RetryDisposition,
    WorkItem,
    WorkSource,
    WorkStatus,
)


def work_item() -> WorkItem:
    return WorkItem(
        work_id="work-retry",
        submission_id="submission-retry",
        account_id="U100001",
        action=WorkAction.REBALANCE,
        source=WorkSource.RETRY,
        status=WorkStatus.PENDING,
        execute_at="2026-11-27T14:35:00.000000Z",
        request_token_hash=hashlib.sha256(b"retry").hexdigest(),
        created_at="2026-11-26T15:00:00.000000Z",
    )


class RetryDecisionTests(unittest.TestCase):
    def test_created_and_reused_require_a_work_item(self):
        for disposition in (RetryDisposition.CREATED, RetryDisposition.REUSED):
            with self.subTest(disposition=disposition):
                with self.assertRaisesRegex(ValidationError, "must contain a Work Item"):
                    RetryDecision(disposition=disposition)

    def test_not_created_rejects_a_work_item(self):
        with self.assertRaisesRegex(ValidationError, "must not contain a Work Item"):
            RetryDecision(disposition=RetryDisposition.NOT_CREATED, item=work_item())

    def test_valid_combinations_construct(self):
        item = work_item()
        self.assertIsNone(RetryDecision(disposition=RetryDisposition.NOT_CREATED).item)
        for disposition in (RetryDisposition.CREATED, RetryDisposition.REUSED):
            self.assertEqual(RetryDecision(disposition=disposition, item=item).item, item)

    def test_reused_requires_pending_and_created_requires_retry_source(self):
        with self.assertRaisesRegex(ValidationError, "pending Work Item"):
            RetryDecision(
                disposition=RetryDisposition.REUSED,
                item=work_item().model_copy(update={"status": WorkStatus.RUNNING}),
            )
        with self.assertRaisesRegex(ValidationError, "retry-source"):
            RetryDecision(
                disposition=RetryDisposition.CREATED,
                item=work_item().model_copy(update={"source": WorkSource.SCHEDULED}),
            )


if __name__ == "__main__":
    unittest.main()

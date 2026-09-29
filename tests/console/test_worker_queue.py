import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.executor import ExecutionResult
from spark_console.models import DouyinAccount, SparkTask, TaskRun, User
from spark_console.worker import Worker, _poll_tasks


class WorkerQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        key = root / "cookie.key"
        key.write_bytes(b"q" * 32)
        self.settings = Settings(root, "sqlite:///:memory:", key, key)
        self.engine = create_engine(self.settings.database_url)
        self.addCleanup(self.engine.dispose)
        create_schema(self.engine)
        self.now = datetime(2026, 9, 28, 18, tzinfo=timezone.utc)
        self.sent = []
        self.result = ExecutionResult(True, "complete")

        async def execute(_payload, target, _message, **_kwargs):
            self.sent.append(target)
            return self.result

        self.executor = SimpleNamespace(execute=execute)
        with Session(self.engine) as db:
            owner = User(username="queue-test", password_hash="unused")
            db.add(owner)
            db.flush()
            sealed = CookieCipher(b"q" * 32).encrypt(b"[]")
            account = DouyinAccount(
                owner_user_id=owner.id,
                display_name="test",
                encrypted_cookies=sealed.ciphertext,
                cookie_nonce=sealed.nonce,
            )
            db.add(account)
            db.flush()
            self.account_id, self.owner_id = account.id, owner.id
            db.commit()

    def add_tasks(self, count):
        with Session(self.engine) as db:
            for index in range(count):
                db.add(SparkTask(
                    id=f"task-{index:02}",
                    owner_user_id=self.owner_id,
                    douyin_account_id=self.account_id,
                    target_name=f"target-{index:02}",
                    send_time="02:00",
                    message_template="test",
                    enabled=True,
                    next_run_at=self.now,
                ))
            db.commit()

    def worker(self, started_at=None):
        return Worker(self.settings, self.engine, executor=self.executor,
                      started_at=started_at or self.now - timedelta(minutes=1))

    async def test_all_eighteen_on_time_tasks_survive_a_long_queue(self):
        self.add_tasks(18)
        worker = self.worker()
        for index in range(18):
            run = await worker.run_once(self.now + timedelta(minutes=index))
            self.assertEqual("success", run.status)
        self.assertEqual(18, len(self.sent))
        self.assertEqual(18, len(set(self.sent)))
        self.assertIsNone(await worker.run_once(self.now + timedelta(minutes=18)))
        with Session(self.engine) as db:
            self.assertTrue(all(task.next_run_at.replace(tzinfo=timezone.utc)
                                == self.now + timedelta(days=1)
                                for task in db.scalars(select(SparkTask))))

    async def test_unobserved_overdue_task_is_still_skipped(self):
        self.add_tasks(1)
        run = await self.worker().run_once(self.now + timedelta(minutes=20))
        self.assertEqual("missed_window", run.error_code)
        self.assertEqual([], self.sent)

    async def test_restart_does_not_replay_an_old_queue(self):
        self.add_tasks(2)
        await self.worker().run_once(self.now)
        restarted = self.worker(self.now + timedelta(minutes=20))
        run = await restarted.run_once(self.now + timedelta(minutes=20))
        self.assertEqual("worker_was_offline", run.error_code)
        self.assertEqual(1, len(self.sent))

    async def test_queue_admission_does_not_live_forever(self):
        self.add_tasks(2)
        worker = self.worker()
        await worker.run_once(self.now)
        run = await worker.run_once(self.now + timedelta(hours=1, seconds=1))
        self.assertEqual("missed_window", run.error_code)
        self.assertEqual(1, len(self.sent))

    async def test_changed_schedule_cannot_reuse_an_old_admission(self):
        self.add_tasks(2)
        worker = self.worker()
        await worker.run_once(self.now)
        with Session(self.engine) as db:
            db.get(SparkTask, "task-01").next_run_at = self.now + timedelta(seconds=1)
            db.commit()
        run = await worker.run_once(self.now + timedelta(minutes=20))
        self.assertEqual("missed_window", run.error_code)
        self.assertEqual(1, len(self.sent))

    async def test_retry_budget_does_not_reset_after_a_long_queue(self):
        self.add_tasks(1)
        self.result = ExecutionResult(False, "selecting_target", "target_not_found",
                                      "not found", retryable=True)
        worker = self.worker()
        first = await worker.run_once(self.now)
        self.assertEqual("retry_scheduled_1m", first.error_code)
        # Simulate the retry waiting behind other admitted work.
        with Session(self.engine) as db:
            worker._admit_due_tasks(db, self.now + timedelta(minutes=1))
        second = await worker.run_once(self.now + timedelta(minutes=20))
        self.assertEqual("retry_scheduled_5m", second.error_code)
        with Session(self.engine) as db:
            worker._admit_due_tasks(db, self.now + timedelta(minutes=25))
        third = await worker.run_once(self.now + timedelta(minutes=40))
        self.assertEqual("target_not_found", third.error_code)
        self.assertEqual(3, len(self.sent))
        with Session(self.engine) as db:
            self.assertEqual(3, len(db.scalars(select(TaskRun)).all()))

    async def test_polling_waits_only_when_queue_is_empty(self):
        stopping = asyncio.Event()
        calls = 0

        async def run_once():
            nonlocal calls
            calls += 1
            return object() if calls < 3 else None

        async def idle_wait(awaitable, *, timeout):
            awaitable.close()
            self.assertEqual(3, calls)
            self.assertEqual(10, timeout)
            stopping.set()

        worker = SimpleNamespace(run_once=run_once, settings=self.settings)
        with patch("spark_console.worker.asyncio.wait_for", side_effect=idle_wait):
            await _poll_tasks(worker, stopping)
        self.assertEqual(3, calls)


if __name__ == "__main__":
    unittest.main()

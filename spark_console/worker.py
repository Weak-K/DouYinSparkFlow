from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.executor import DouyinExecutor
from spark_console.models import (
    DouyinAccount,
    DouyinConversation,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskRun,
    User,
    WorkerLock,
)
from spark_console.notify import MailSettings, notify_cookie_expired
from spark_console.scheduler import claim_next_due_task, finish_run
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService


logger = logging.getLogger("spark.worker")


class Worker:
    STARTUP_GRACE = timedelta(minutes=5)
    RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5))
    EXECUTION_TIMEOUT_SECONDS = 180
    # 好友快照超过这个时长就借下一次执行顺手刷新一次（每个账号每天最多一次）。
    SNAPSHOT_MAX_AGE = timedelta(hours=20)

    def __init__(
        self,
        settings: Settings,
        engine,
        executor=None,
        clock_offset_seconds=0.0,
        started_at: datetime | None = None,
        execution_timeout_seconds: float | None = None,
        mail_settings: MailSettings | None = None,
        snapshot_max_age: timedelta | None = None,
    ):
        self.settings = settings
        self.engine = engine
        self.executor = executor or DouyinExecutor()
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.clock_offset_seconds = clock_offset_seconds
        self.started_at = started_at or datetime.now(timezone.utc)
        self.execution_timeout_seconds = (
            execution_timeout_seconds or self.EXECUTION_TIMEOUT_SECONDS
        )
        self.mail_settings = mail_settings or MailSettings.from_environ(os.environ)
        self.snapshot_max_age = snapshot_max_age or timedelta(
            hours=float(
                os.environ.get(
                    "SPARK_SNAPSHOT_MAX_AGE_HOURS",
                    str(self.SNAPSHOT_MAX_AGE.total_seconds() / 3600),
                )
            )
        )
        self.cipher = CookieCipher(settings.cookie_key_file.read_bytes())
        self._recover_interrupted_runs()

    def _recover_interrupted_runs(self) -> None:
        retry_at = self.started_at + self.RETRY_DELAYS[0]
        with session_scope(self.engine) as db:
            interrupted = db.scalars(
                select(TaskRun).where(
                    TaskRun.status == "running",
                    TaskRun.finished_at.is_(None),
                )
            ).all()
            for run in interrupted:
                finish_run(
                    run,
                    "failed",
                    "worker_restart",
                    self.started_at,
                    "worker_interrupted",
                    "执行器重启中断了任务，已安排 1 分钟后重试",
                )
                task = db.get(SparkTask, run.task_id)
                if task is not None and task.enabled:
                    task.next_run_at = retry_at

    async def run_once(self, now: datetime | None = None):
        current_time = now or datetime.now(timezone.utc)
        with session_scope(self.engine) as db:
            lock = db.get(WorkerLock, 1)
            if lock is None:
                lock = WorkerLock(id=1)
                db.add(lock)
            lock.worker_id = self.worker_id
            lock.lease_until = current_time + timedelta(
                seconds=max(30, self.settings.worker_poll_seconds * 3)
            )
            run = claim_next_due_task(db, current_time, self.worker_id)
            if run is None:
                return None
            lock.lease_until = current_time + timedelta(
                seconds=max(
                    30,
                    int(self.execution_timeout_seconds)
                    + self.settings.worker_poll_seconds * 3,
                )
            )
            task = db.get(SparkTask, run.task_id)
            if abs(self.clock_offset_seconds) > self.settings.clock_offset_limit_seconds:
                return finish_run(run, "failed", "clock_check", current_time, "system_time_unhealthy", "服务器时间未同步")
            scheduled = run.scheduled_for if run.scheduled_for.tzinfo else run.scheduled_for.replace(tzinfo=timezone.utc)
            if scheduled < self.started_at and current_time - scheduled > self.STARTUP_GRACE:
                return finish_run(
                    run,
                    "skipped",
                    "missed_startup",
                    current_time,
                    "worker_was_offline",
                    "执行器离线期间任务已错过，未补发",
                )
            if current_time - scheduled > timedelta(minutes=10):
                return finish_run(run, "skipped", "late", current_time, "missed_window", "任务已超过 10 分钟发送窗口")
            account_service = AccountService(db, self.cipher, AuditService(db))
            account = db.get(DouyinAccount, task.douyin_account_id)
            credential_version = account.cookie_version
            target_identity = db.get(SparkTaskTargetIdentity, task.id)
            target_sec_uid = target_identity.sec_uid if target_identity else None
            cookies = account_service.decrypt_for_worker(task.douyin_account_id)
            # 快照过期时借这次执行顺带把好友名单刷新一遍（不额外开浏览器）。
            refresh_targets = self._snapshot_is_stale(db, account.id, current_time)
            run_id = run.id
            task_id = task.id
            account_id = account.id
            target_name = task.target_name
            message_template = task.message_template

        try:
            timed_out = False
            try:
                result = await asyncio.wait_for(
                    self.executor.execute(
                        cookies,
                        target_name,
                        message_template,
                        credential_version=credential_version,
                        target_sec_uid=target_sec_uid,
                        refresh_targets=refresh_targets,
                    ),
                    timeout=self.execution_timeout_seconds,
                )
            except TimeoutError:
                timed_out = True
                result = None
            except Exception:
                result = None
        finally:
            cookies[:] = b"\0" * len(cookies)
            cookies.clear()

        alert: tuple[str, str, str] | None = None
        with session_scope(self.engine) as db:
            run = db.get(TaskRun, run_id)
            task = db.get(SparkTask, task_id)
            if timed_out:
                outcome = finish_run(
                    run,
                    "failed",
                    "worker_timeout",
                    datetime.now(timezone.utc),
                    "execution_timeout",
                    "页面操作超过 3 分钟，已终止本次执行",
                )
            elif result is None:
                outcome = finish_run(
                    run,
                    "failed",
                    "worker_error",
                    datetime.now(timezone.utc),
                    "unexpected_error",
                    "任务执行发生意外异常，Worker 已继续运行",
                )
            else:
                if result.success:
                    db.execute(
                        update(DouyinAccount)
                        .where(DouyinAccount.id == account_id)
                        .values(
                            validation_state="valid",
                            last_verified_at=datetime.now(timezone.utc),
                        )
                    )
                elif result.error_code == "cookie_invalid":
                    alert = self._mark_cookie_invalid(db, account_id)
                retry = None
                if not result.success and result.retryable:
                    retry = self._schedule_retry(
                        db, task, run, current_time, result.stage
                    )
                outcome = (
                    retry
                    if retry is not None
                    else finish_run(
                        run,
                        "success" if result.success else "failed",
                        result.stage,
                        datetime.now(timezone.utc),
                        result.error_code,
                        result.error_summary,
                    )
                )
            if result is not None and (
                result.discovered_names or result.discovered_identities
            ):
                try:
                    AccountService(db, self.cipher, AuditService(db)).record_discovery(
                        account_id,
                        result.discovered_names,
                        result.discovered_identities,
                    )
                except Exception:
                    logger.warning("好友快照写回失败，已忽略以免影响本次执行结果")
        if alert is not None:
            await self._notify_cookie_expired(alert)
        return outcome

    def _snapshot_is_stale(self, db, account_id: str, now: datetime) -> bool:
        """好友快照是否该刷新了（没有任何快照也算过期）。"""

        newest = db.scalar(
            select(func.max(DouyinConversation.discovered_at)).where(
                DouyinConversation.account_id == account_id
            )
        )
        if newest is None:
            return True
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        return now - newest > self.snapshot_max_age

    def _mark_cookie_invalid(
        self, db, account_id: str
    ) -> tuple[str | None, str, str] | None:
        """把抖音号标记为 Cookie 失效，并返回本次需要通知的收件人信息。

        只有在该号由「非失效」变为「失效」时才返回，避免每天定时任务把同一封
        失效邮件反复发出去。所属用户没填邮箱时收件人为 None，
        由 notify 层兜底发给运维收件人。
        """

        row = db.execute(
            select(DouyinAccount, User.email, User.username)
            .join(User, DouyinAccount.owner_user_id == User.id)
            .where(DouyinAccount.id == account_id)
        ).first()
        if row is None:
            return None
        account, owner_email, owner_username = row
        already_invalid = account.validation_state == "invalid"
        account_name = account.display_name
        db.execute(
            update(DouyinAccount)
            .where(DouyinAccount.id == account_id)
            .values(validation_state="invalid")
        )
        if already_invalid:
            return None
        return (owner_email, owner_username, account_name)

    async def _notify_cookie_expired(
        self, alert: tuple[str | None, str, str]
    ) -> bool:
        recipient, owner_username, account_name = alert
        try:
            occurred_at = datetime.now(timezone.utc).astimezone(
                ZoneInfo(self.settings.timezone)
            ).strftime("%Y-%m-%d %H:%M:%S %Z")
        except (ZoneInfoNotFoundError, ValueError):
            occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            sent = await asyncio.to_thread(
                notify_cookie_expired,
                self.mail_settings,
                recipient,
                owner_username=owner_username,
                account_name=account_name,
                occurred_at=occurred_at,
            )
        except Exception:
            logger.exception("Cookie 失效通知发送异常，已忽略以免影响 Worker")
            return False
        if sent:
            logger.info("Cookie 失效通知已发送")
        return sent

    def _schedule_retry(self, db, task, run, now, stage):
        retry_codes = tuple(
            f"retry_scheduled_{int(delay.total_seconds() // 60)}m"
            for delay in self.RETRY_DELAYS
        )
        used_codes = set(
            db.scalars(
                select(TaskRun.error_code).where(
                    TaskRun.task_id == task.id,
                    TaskRun.started_at >= now - timedelta(minutes=15),
                    TaskRun.error_code.in_(retry_codes),
                )
            ).all()
        )
        for delay, code in zip(self.RETRY_DELAYS, retry_codes):
            if code in used_codes:
                continue
            minutes = int(delay.total_seconds() // 60)
            task.next_run_at = now + delay
            return finish_run(
                run,
                "failed",
                stage,
                now,
                code,
                f"发送前遇到临时故障，已安排 {minutes} 分钟后重试",
            )
        return None


async def run_loop() -> None:
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    worker = Worker(settings, engine, clock_offset_seconds=float(os.environ.get("SPARK_CLOCK_OFFSET_SECONDS", "0")))
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            pass
    while not stopping.is_set():
        await worker.run_once()
        try:
            await asyncio.wait_for(stopping.wait(), timeout=settings.worker_poll_seconds)
        except TimeoutError:
            continue


if __name__ == "__main__":
    asyncio.run(run_loop())

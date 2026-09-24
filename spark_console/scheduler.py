from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spark_console.models import SparkTask, TaskRun


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def compute_next_run(send_time: str, now_utc: datetime) -> datetime:
    hour, minute = map(int, send_time.split(":"))
    local_now = _utc(now_utc).astimezone(ZoneInfo("Asia/Shanghai"))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def claim_next_due_task(
    session: Session, now: datetime, worker_id: str
) -> TaskRun | None:
    now_utc = _utc(now)
    task = session.scalar(
        select(SparkTask)
        .where(
            SparkTask.enabled.is_(True),
            SparkTask.douyin_account_id.is_not(None),
            SparkTask.next_run_at.is_not(None),
            SparkTask.next_run_at <= now_utc,
        )
        .order_by(SparkTask.next_run_at, SparkTask.id)
        .limit(1)
    )
    if task is None:
        return None
    scheduled_for = _utc(task.next_run_at)
    run = TaskRun(
        task_id=task.id,
        scheduled_for=scheduled_for,
        status="running",
        stage="claimed",
        started_at=now_utc,
    )
    session.add(run)
    task.next_run_at = compute_next_run(task.send_time, now_utc)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return run


def finish_run(
    run: TaskRun,
    status: str,
    stage: str,
    now: datetime,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> TaskRun:
    run.status = status
    run.stage = stage
    run.finished_at = _utc(now)
    run.error_code = error_code
    run.error_summary = error_summary[:240] if error_summary else None
    if status == "failed":
        # 本仓库新增：失败（含 Cookie 失效）时发告警邮件。
        # 放在这里是因为 finish_run 是所有失败路径的唯一收尾点；notify 内部
        # 异步发送且有冷却窗口，不会阻塞当前事务，并会自行跳过 cookie_invalid
        # 与「已安排重试」的中间态。详见 spark_console/notify.py。
        from spark_console import notify

        notify.alert_task_failure(
            stage=stage,
            error_code=error_code or "",
            error_summary=run.error_summary or "",
            task_id=run.task_id or "",
        )
    return run

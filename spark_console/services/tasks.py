from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spark_console.models import DouyinContactIdentity, SparkTask, SparkTaskTargetIdentity
from spark_console.services import Conflict, NotFound, ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class TaskService:
    def __init__(self, session: Session, accounts: AccountService, audit: AuditService):
        self.session = session
        self.accounts = accounts
        self.audit = audit

    def get_owned(self, owner_id: str, task_id: str) -> SparkTask:
        task = self.session.scalar(
            select(SparkTask).where(
                SparkTask.id == task_id, SparkTask.owner_user_id == owner_id
            )
        )
        if task is None:
            raise NotFound("task not found")
        return task

    def list_owned(self, owner_id: str) -> list[SparkTask]:
        return list(
            self.session.scalars(
                select(SparkTask)
                .where(SparkTask.owner_user_id == owner_id)
                .order_by(SparkTask.send_time, SparkTask.created_at)
            ).all()
        )

    def create(
        self,
        owner_id: str,
        account_id: str,
        target_name: str,
        send_time: str,
        message_template: str,
        target_sec_uid: str | None = None,
    ) -> SparkTask:
        target = target_name.strip()
        message = message_template.strip()
        if not target or len(target) > 64:
            raise ValidationError("好友名称须为 1–64 个字符")
        if not _TIME_RE.fullmatch(send_time):
            raise ValidationError("发送时间格式必须为 HH:MM")
        if not message or len(message) > 500:
            raise ValidationError("消息内容须为 1–500 个字符")
        account = self.accounts.get_owned(owner_id, account_id)
        stable_target = str(target_sec_uid or "").strip()
        if stable_target and self.session.get(
            DouyinContactIdentity, (account.id, stable_target)
        ) is None:
            raise ValidationError("所选好友不属于当前抖音账号")
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour, minute = map(int, send_time.split(":"))
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            from datetime import timedelta
            candidate += timedelta(days=1)
        task = SparkTask(
            owner_user_id=owner_id,
            douyin_account_id=account.id,
            target_name=target,
            send_time=send_time,
            message_template=message,
            enabled=True,
            next_run_at=candidate.astimezone(timezone.utc),
        )
        self.session.add(task)
        try:
            self.session.flush()
            if stable_target:
                self.session.add(
                    SparkTaskTargetIdentity(
                        task_id=task.id,
                        sec_uid=stable_target,
                    )
                )
                self.session.flush()
        except IntegrityError as error:
            self.session.rollback()
            raise Conflict("相同账号、好友和时间的启用任务已存在") from error
        self.audit.write(owner_id, "task.created", "spark_task", task.id)
        return task

    def create_many(
        self,
        owner_id: str,
        account_id: str,
        targets: list[tuple[str, str]],
        send_time: str,
        message_template: str,
    ) -> tuple[list[SparkTask], list[tuple[str, str]]]:
        """一次给多位好友建任务（同一个账号、时间、内容）。

        返回 ``(已创建的任务, [(好友名, 跳过原因), ...])``。

        每条**成功即 commit、失败即 rollback**，所以单个好友出错（该时间已有任务、
        昵称不合法、不属于该账号等）只丢掉它自己，不会连累同批已建好的任务
        —— 否则 create() 里那次整事务 rollback 会把前面的成果一起清空。
        """

        created: list[SparkTask] = []
        skipped: list[tuple[str, str]] = []
        for name, sec_uid in targets:
            try:
                task = self.create(
                    owner_id,
                    account_id,
                    name,
                    send_time,
                    message_template,
                    target_sec_uid=sec_uid,
                )
            except Conflict:
                self.session.rollback()
                skipped.append((name, "该时间已有发往此好友的启用任务"))
            except ValidationError as error:
                self.session.rollback()
                skipped.append((name, str(error)))
            else:
                self.session.commit()
                created.append(task)
        return created, skipped

    def set_enabled_owned(self, owner_id: str, task_id: str, enabled: bool) -> SparkTask:
        task = self.get_owned(owner_id, task_id)
        if enabled and task.douyin_account_id is None:
            raise ValidationError("账号已删除，无法启用任务")
        task.enabled = enabled
        self.audit.write(owner_id, "task.enabled" if enabled else "task.disabled", "spark_task", task.id)
        return task

    def update_owned(
        self,
        owner_id: str,
        task_id: str,
        account_id: str,
        target_name: str,
        send_time: str,
        message_template: str,
        target_sec_uid: str | None = None,
    ) -> SparkTask:
        task = self.get_owned(owner_id, task_id)
        target = target_name.strip()
        message = message_template.strip()
        if not target or len(target) > 64:
            raise ValidationError("好友名称须为 1–64 个字符")
        if not _TIME_RE.fullmatch(send_time):
            raise ValidationError("发送时间格式必须为 HH:MM")
        if not message or len(message) > 500:
            raise ValidationError("消息内容须为 1–500 个字符")
        account = self.accounts.get_owned(owner_id, account_id)
        stable_target = str(target_sec_uid or "").strip()
        if stable_target and self.session.get(
            DouyinContactIdentity, (account.id, stable_target)
        ) is None:
            raise ValidationError("所选好友不属于当前抖音账号")
        if task.enabled:
            duplicate = self.session.scalar(
                select(SparkTask.id).where(
                    SparkTask.id != task.id,
                    SparkTask.douyin_account_id == account.id,
                    SparkTask.target_name == target,
                    SparkTask.send_time == send_time,
                    SparkTask.enabled.is_(True),
                )
            )
            if duplicate is not None:
                raise Conflict("相同账号、好友和时间的启用任务已存在")

        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour, minute = map(int, send_time.split(":"))
        candidate = local_now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= local_now:
            from datetime import timedelta

            candidate += timedelta(days=1)
        task.douyin_account_id = account.id
        task.target_name = target
        task.send_time = send_time
        task.message_template = message
        task.next_run_at = candidate.astimezone(timezone.utc)

        binding = self.session.get(SparkTaskTargetIdentity, task.id)
        if stable_target:
            if binding is None:
                self.session.add(
                    SparkTaskTargetIdentity(task_id=task.id, sec_uid=stable_target)
                )
            else:
                binding.sec_uid = stable_target
        elif binding is not None:
            self.session.delete(binding)
        self.audit.write(owner_id, "task.updated", "spark_task", task.id)
        return task

    def delete_owned(self, owner_id: str, task_id: str) -> None:
        task = self.get_owned(owner_id, task_id)
        self.session.delete(task)
        self.audit.write(owner_id, "task.deleted", "spark_task", task_id)

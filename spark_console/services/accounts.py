from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from spark_console.credentials import CredentialError, CredentialPayload
from spark_console.crypto import CookieCipher
from spark_console.models import (
    DouyinAccount,
    DouyinAccountIdentity,
    DouyinContactIdentity,
    DouyinConversation,
    SparkTask,
    utc_now,
)
from spark_console.services import NotFound, ValidationError
from spark_console.services.audits import AuditService


class AccountService:
    def __init__(self, session: Session, cipher: CookieCipher, audit: AuditService):
        self.session = session
        self.cipher = cipher
        self.audit = audit

    def create(self, owner_id: str, display_name: str, cookies: bytes | str) -> DouyinAccount:
        name = self._validated_display_name(display_name)
        raw = cookies.encode("utf-8") if isinstance(cookies, str) else cookies
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValidationError("Cookie 必须是有效的 JSON") from error
        if not isinstance(parsed, list) or not parsed:
            raise ValidationError("Cookie JSON 必须是非空数组")
        sealed = self.cipher.encrypt(raw)
        account = DouyinAccount(
            owner_user_id=owner_id,
            display_name=name,
            encrypted_cookies=sealed.ciphertext,
            cookie_nonce=sealed.nonce,
        )
        self.session.add(account)
        self.session.flush()
        self.audit.write(owner_id, "account.created", "douyin_account", account.id)
        return account

    def create_from_storage_state(
        self,
        owner_id: str,
        display_name: str,
        storage_state: dict,
        douyin_unique_id: str | None = None,
        conversation_names=(),
        contact_identities=(),
    ) -> DouyinAccount:
        name = self._validated_display_name(display_name)
        normalized_unique_id = (
            douyin_unique_id.strip() if douyin_unique_id is not None else None
        )
        if normalized_unique_id == "":
            normalized_unique_id = None
        if normalized_unique_id is not None and len(normalized_unique_id) > 64:
            raise ValidationError("抖音号不能超过 64 个字符")

        envelope = {"version": 2, "storage_state": storage_state}
        try:
            raw = json.dumps(
                envelope, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            CredentialPayload.parse(raw, 2)
        except (CredentialError, TypeError, ValueError, UnicodeEncodeError):
            raise ValidationError("浏览器凭据格式无效") from None
        sealed = self.cipher.encrypt(raw)
        account = DouyinAccount(
            owner_user_id=owner_id,
            display_name=name,
            encrypted_cookies=sealed.ciphertext,
            cookie_nonce=sealed.nonce,
            cookie_version=2,
            validation_state="valid",
            last_verified_at=utc_now(),
        )
        self.session.add(account)
        self.session.flush()
        self.session.add(
            DouyinAccountIdentity(
                account_id=account.id, douyin_unique_id=normalized_unique_id
            )
        )
        seen = set()
        for display_name in conversation_names:
            normalized_name = str(display_name).strip()
            if not normalized_name or normalized_name in seen:
                continue
            seen.add(normalized_name)
            self.session.add(
                DouyinConversation(
                    account_id=account.id,
                    display_name=normalized_name[:256],
                )
            )
        for identity in contact_identities:
            sec_uid = str(identity.sec_uid).strip()
            if not sec_uid:
                continue
            self.session.add(
                DouyinContactIdentity(
                    account_id=account.id,
                    sec_uid=sec_uid[:256],
                    short_id=_limited(identity.short_id, 64),
                    unique_id=_limited(identity.unique_id, 128),
                    nickname=_limited(identity.nickname, 256),
                    remark_name=_limited(identity.remark_name, 256),
                )
            )
        self.audit.write(owner_id, "account.created", "douyin_account", account.id)
        return account

    def rename_owned(
        self, owner_id: str, account_id: str, display_name: str
    ) -> DouyinAccount:
        account = self.get_owned(owner_id, account_id)
        account.display_name = self._validated_display_name(display_name)
        self.session.flush()
        self.audit.write(owner_id, "account.renamed", "douyin_account", account.id)
        return account

    def get_owned(self, owner_id: str, account_id: str) -> DouyinAccount:
        account = self.session.scalar(
            select(DouyinAccount).where(
                DouyinAccount.id == account_id,
                DouyinAccount.owner_user_id == owner_id,
            )
        )
        if account is None:
            raise NotFound("account not found")
        return account

    def list_owned(self, owner_id: str) -> list[dict[str, str]]:
        accounts = self.session.scalars(
            select(DouyinAccount)
            .where(DouyinAccount.owner_user_id == owner_id)
            .order_by(DouyinAccount.created_at)
        ).all()
        return [
            {"id": item.id, "display_name": item.display_name, "validation_state": item.validation_state}
            for item in accounts
        ]

    def record_discovery(
        self,
        account_id: str,
        conversation_names=(),
        contact_identities=(),
    ) -> int:
        """把一次真实执行中看到的好友名单并回快照表（只增不删）。

        控制台的任务下拉框完全依赖这张快照，而老实现只在扫码登录那一刻拍一张
        首屏快照，之后永不更新——好友改名、新人加进来都看不见。这里做 upsert：
        新名字补进来，老名字只刷新 discovered_at，绝不删除（删了会连用户已绑定的
        选项一起丢）。
        """

        account = self.session.get(DouyinAccount, account_id)
        if account is None:
            return 0
        now = utc_now()
        written = 0
        known = set(
            self.session.scalars(
                select(DouyinConversation.display_name).where(
                    DouyinConversation.account_id == account_id
                )
            ).all()
        )
        for display_name in conversation_names:
            normalized_name = str(display_name).strip()[:256]
            if not normalized_name:
                continue
            if normalized_name in known:
                self.session.execute(
                    update(DouyinConversation)
                    .where(
                        DouyinConversation.account_id == account_id,
                        DouyinConversation.display_name == normalized_name,
                    )
                    .values(discovered_at=now)
                )
            else:
                known.add(normalized_name)
                self.session.add(
                    DouyinConversation(
                        account_id=account_id, display_name=normalized_name
                    )
                )
            written += 1

        seen_sec_uids = set()
        for identity in contact_identities:
            sec_uid = str(getattr(identity, "sec_uid", "") or "").strip()[:256]
            if not sec_uid or sec_uid in seen_sec_uids:
                continue
            seen_sec_uids.add(sec_uid)
            row = self.session.get(DouyinContactIdentity, (account_id, sec_uid))
            if row is None:
                self.session.add(
                    DouyinContactIdentity(
                        account_id=account_id,
                        sec_uid=sec_uid,
                        short_id=_limited(getattr(identity, "short_id", None), 64),
                        unique_id=_limited(getattr(identity, "unique_id", None), 128),
                        nickname=_limited(getattr(identity, "nickname", None), 256),
                        remark_name=_limited(
                            getattr(identity, "remark_name", None), 256
                        ),
                    )
                )
            else:
                # 用非空值覆盖，别让一次没带昵称的响应把已知信息抹掉。
                for field, length in (
                    ("short_id", 64),
                    ("unique_id", 128),
                    ("nickname", 256),
                    ("remark_name", 256),
                ):
                    value = _limited(getattr(identity, field, None), length)
                    if value:
                        setattr(row, field, value)
                row.discovered_at = now
            written += 1
        self.session.flush()
        return written

    def decrypt_for_worker(self, account_id: str) -> bytearray:
        account = self.session.get(DouyinAccount, account_id)
        if account is None:
            raise NotFound("account not found")
        return bytearray(self.cipher.decrypt(account.encrypted_cookies, account.cookie_nonce))

    def delete_owned(self, owner_id: str, account_id: str) -> None:
        account = self.get_owned(owner_id, account_id)
        self.session.execute(
            update(SparkTask)
            .where(SparkTask.douyin_account_id == account.id)
            .values(enabled=False, douyin_account_id=None)
        )
        account.encrypted_cookies = b""
        account.cookie_nonce = b""
        self.session.flush()
        self.session.delete(account)
        self.audit.write(owner_id, "account.deleted", "douyin_account", account_id)

    @staticmethod
    def _validated_display_name(display_name: str) -> str:
        name = display_name.strip()
        if not name or len(name) > 64:
            raise ValidationError("账号名称须为 1–64 个字符")
        return name


def _limited(value, length: int) -> str | None:
    text = str(value or "").strip()
    return text[:length] or None

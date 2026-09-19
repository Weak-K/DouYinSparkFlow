import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spark_console.notify import (
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_SMTP_HOST,
    DEFAULT_SMTP_PORT,
    MailSettings,
    alert_task_failure,
    notify_cookie_expired,
    parse_recipients,
    send_mail,
)


ALERTS = ("ops@example.com",)
CONFIGURED = MailSettings(
    host="smtp.example.com",
    port=465,
    username="sender@example.com",
    password="secret",
    sender="sender@example.com",
    alert_recipients=ALERTS,
)


class MailSettingsTests(unittest.TestCase):
    def test_missing_credentials_are_not_configured(self):
        settings = MailSettings.from_environ({})

        self.assertFalse(settings.configured)
        self.assertEqual(DEFAULT_SMTP_HOST, settings.host)
        self.assertEqual(DEFAULT_SMTP_PORT, settings.port)
        self.assertEqual((), settings.alert_recipients)
        self.assertEqual(DEFAULT_COOLDOWN_MINUTES, settings.cooldown_minutes)

    def test_environ_overrides_host_port_security_and_sender(self):
        settings = MailSettings.from_environ(
            {
                "SPARK_SMTP_HOST": "smtp.example.com",
                "SPARK_SMTP_PORT": "587",
                "SPARK_SMTP_SECURITY": "starttls",
                "SPARK_SMTP_USER": "sender@example.com",
                "SPARK_SMTP_PASSWORD": "secret",
            }
        )

        self.assertTrue(settings.configured)
        self.assertEqual(
            ("smtp.example.com", 587, "starttls"),
            (settings.host, settings.port, settings.security),
        )
        self.assertEqual("sender@example.com", settings.sender)

    def test_explicit_from_address_wins_and_bad_values_fall_back(self):
        settings = MailSettings.from_environ(
            {
                "SPARK_SMTP_PORT": "not-a-port",
                "SPARK_SMTP_SECURITY": "carrier-pigeon",
                "SPARK_SMTP_USER": "sender@example.com",
                "SPARK_SMTP_PASSWORD": "secret",
                "SPARK_SMTP_FROM": "alerts@example.com",
                "SPARK_ALERT_COOLDOWN_MINUTES": "15",
            }
        )

        self.assertEqual(("ssl", DEFAULT_SMTP_PORT), (settings.security, settings.port))
        self.assertEqual("alerts@example.com", settings.sender)
        self.assertEqual(15, settings.cooldown_minutes)

    def test_recipients_split_on_commas_semicolons_and_whitespace(self):
        settings = MailSettings.from_environ(
            {"SPARK_ALERT_EMAIL_TO": " a@example.com, b@example.com;c@example.com "}
        )

        self.assertEqual(
            ("a@example.com", "b@example.com", "c@example.com"),
            settings.alert_recipients,
        )
        self.assertEqual((), parse_recipients("   "))


class SendMailTests(unittest.TestCase):
    def test_unconfigured_smtp_skips_sending_without_touching_the_network(self):
        with self.assertLogs("spark.notify", level="WARNING"), patch(
            "spark_console.notify.smtplib.SMTP_SSL"
        ) as smtp:
            sent = send_mail(MailSettings(), "owner@example.com", "主题", "正文")

        self.assertFalse(sent)
        smtp.assert_not_called()

    def test_empty_recipient_is_skipped(self):
        with self.assertLogs("spark.notify", level="WARNING"), patch(
            "spark_console.notify.smtplib.SMTP_SSL"
        ) as smtp:
            sent = send_mail(CONFIGURED, [], "主题", "正文")

        self.assertFalse(sent)
        smtp.assert_not_called()

    def test_send_mail_reaches_only_the_owner_address(self):
        with patch("spark_console.notify.smtplib.SMTP_SSL") as smtp:
            sent = send_mail(CONFIGURED, "owner@example.com", "【火花控制台】测试", "正文")

        self.assertTrue(sent)
        server = smtp.return_value.__enter__.return_value
        server.login.assert_called_once_with("sender@example.com", "secret")
        sender, recipients, _payload = server.sendmail.call_args.args
        self.assertEqual("sender@example.com", sender)
        self.assertEqual(["owner@example.com"], recipients)

    def test_send_mail_supports_multiple_recipients(self):
        with patch("spark_console.notify.smtplib.SMTP_SSL") as smtp:
            send_mail(CONFIGURED, ["a@example.com", "b@example.com"], "主题", "正文")

        server = smtp.return_value.__enter__.return_value
        self.assertEqual(
            ["a@example.com", "b@example.com"], server.sendmail.call_args.args[1]
        )

    def test_starttls_uses_plain_smtp_with_starttls_upgrade(self):
        settings = MailSettings(
            host="smtp.example.com",
            port=587,
            username="sender@example.com",
            password="secret",
            sender="sender@example.com",
            security="starttls",
        )
        with patch("spark_console.notify.smtplib.SMTP") as smtp:
            sent = send_mail(settings, "owner@example.com", "主题", "正文")

        self.assertTrue(sent)
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        server.login.assert_called_once()

    def test_smtp_failures_are_swallowed(self):
        with self.assertLogs("spark.notify", level="WARNING"), patch(
            "spark_console.notify.smtplib.SMTP_SSL", side_effect=OSError("boom")
        ):
            sent = send_mail(CONFIGURED, "owner@example.com", "主题", "正文")

        self.assertFalse(sent)


class CookieAlertTests(unittest.TestCase):
    def test_cookie_alert_targets_the_account_owner_only(self):
        with patch("spark_console.notify.send_mail", return_value=True) as sender:
            sent = notify_cookie_expired(
                CONFIGURED,
                "owner@example.com",
                owner_username="friend",
                account_name="我的小号",
                occurred_at="2026-09-20 09:00:00 CST",
            )

        self.assertTrue(sent)
        recipients, subject, body = sender.call_args.args[1:]
        self.assertEqual(["owner@example.com"], recipients)
        self.assertNotIn(ALERTS[0], recipients)
        self.assertIn("我的小号", subject)
        self.assertIn("friend", body)
        self.assertIn("重新扫码绑定", body)

    def test_cookie_alert_falls_back_to_ops_recipients_without_a_user_address(self):
        with patch("spark_console.notify.send_mail", return_value=True) as sender:
            notify_cookie_expired(
                CONFIGURED,
                None,
                owner_username="legacy-owner",
                account_name="旧账号",
                occurred_at="2026-09-20 09:00:00 CST",
            )

        recipients, _subject, body = sender.call_args.args[1:]
        self.assertEqual(list(ALERTS), recipients)
        self.assertIn("尚未填写通知邮箱", body)

    def test_cookie_alert_is_skipped_when_no_address_is_available_at_all(self):
        with self.assertLogs("spark.notify", level="WARNING"), patch(
            "spark_console.notify.send_mail"
        ) as sender:
            sent = notify_cookie_expired(
                MailSettings(),
                None,
                owner_username="legacy-owner",
                account_name="旧账号",
                occurred_at="2026-09-20 09:00:00 CST",
            )

        self.assertFalse(sent)
        sender.assert_not_called()

    def test_cookie_alert_includes_optional_detail(self):
        with patch("spark_console.notify.send_mail", return_value=True) as sender:
            notify_cookie_expired(
                CONFIGURED,
                "owner@example.com",
                owner_username="friend",
                account_name="小号",
                occurred_at="2026-09-20 09:00:00 CST",
                detail="redirected_to_login",
            )

        self.assertIn("redirected_to_login", sender.call_args.args[3])


class TaskFailureAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = MailSettings(
            host="smtp.example.com",
            username="sender@example.com",
            password="secret",
            sender="sender@example.com",
            alert_recipients=ALERTS,
            data_dir=self.temp.name,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_cookie_invalid_is_left_to_the_per_user_notification(self):
        with patch("spark_console.notify._send_async") as dispatcher:
            sent = alert_task_failure(
                stage="authenticating", error_code="cookie_invalid", settings=self.settings
            )

        self.assertFalse(sent)
        dispatcher.assert_not_called()

    def test_other_failures_notify_ops_recipients(self):
        with patch("spark_console.notify._send_async") as dispatcher:
            sent = alert_task_failure(
                stage="navigation", error_code="network_unavailable", settings=self.settings
            )

        self.assertTrue(sent)
        _settings, recipients, subject, body = dispatcher.call_args.args
        self.assertEqual(ALERTS, tuple(recipients))
        self.assertIn("network_unavailable", subject)
        self.assertIn("navigation", body)

    def test_repeated_failures_inside_the_cooldown_are_suppressed(self):
        with patch("spark_console.notify._send_async") as dispatcher:
            first = alert_task_failure(error_code="network_unavailable", settings=self.settings)
            second = alert_task_failure(error_code="network_unavailable", settings=self.settings)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(1, dispatcher.call_count)
        state = json.loads((Path(self.temp.name) / "alert-state.json").read_text("utf-8"))
        self.assertEqual(1, state["suppressed"])

        with patch("spark_console.notify._send_async") as dispatcher:
            third = alert_task_failure(error_code="network_unavailable", settings=self.settings)
        self.assertFalse(third)
        self.assertEqual(0, dispatcher.call_count)

    def test_unconfigured_settings_never_dispatch(self):
        with patch("spark_console.notify._send_async") as dispatcher:
            sent = alert_task_failure(
                error_code="network_unavailable", settings=MailSettings()
            )

        self.assertFalse(sent)
        dispatcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()

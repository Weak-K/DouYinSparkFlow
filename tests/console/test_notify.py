import unittest
from unittest.mock import patch

from spark_console.notify import (
    DEFAULT_SMTP_HOST,
    DEFAULT_SMTP_PORT,
    MailSettings,
    notify_cookie_expired,
    send_mail,
)


CONFIGURED = MailSettings(
    host="smtp.example.com",
    port=465,
    username="sender@example.com",
    password="secret",
    sender="sender@example.com",
)


class MailSettingsTests(unittest.TestCase):
    def test_missing_credentials_are_not_configured(self):
        settings = MailSettings.from_environ({})

        self.assertFalse(settings.configured)
        self.assertEqual(DEFAULT_SMTP_HOST, settings.host)
        self.assertEqual(DEFAULT_SMTP_PORT, settings.port)

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
            }
        )

        self.assertEqual(("ssl", DEFAULT_SMTP_PORT), (settings.security, settings.port))
        self.assertEqual("alerts@example.com", settings.sender)


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
            sent = send_mail(CONFIGURED, "", "主题", "正文")

        self.assertFalse(sent)
        smtp.assert_not_called()

    def test_send_mail_reaches_only_the_owner_address(self):
        with patch("spark_console.notify.smtplib.SMTP_SSL") as smtp:
            sent = send_mail(CONFIGURED, "owner@example.com", "[火花守护] 测试", "正文")

        self.assertTrue(sent)
        server = smtp.return_value.__enter__.return_value
        server.login.assert_called_once_with("sender@example.com", "secret")
        sender, recipients, _payload = server.sendmail.call_args.args
        self.assertEqual("sender@example.com", sender)
        self.assertEqual(["owner@example.com"], recipients)

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
        with self.assertLogs("spark.notify", level="ERROR"), patch(
            "spark_console.notify.smtplib.SMTP_SSL", side_effect=OSError("boom")
        ):
            sent = send_mail(CONFIGURED, "owner@example.com", "主题", "正文")

        self.assertFalse(sent)


class CookieAlertTests(unittest.TestCase):
    def test_cookie_alert_targets_the_account_owner(self):
        with patch("spark_console.notify.send_mail", return_value=True) as sender:
            sent = notify_cookie_expired(
                CONFIGURED,
                "owner@example.com",
                owner_username="friend",
                account_name="我的小号",
                occurred_at="2026-09-20 09:00:00 CST",
            )

        self.assertTrue(sent)
        recipient, subject, body = sender.call_args.args[1:]
        self.assertEqual("owner@example.com", recipient)
        self.assertIn("我的小号", subject)
        self.assertIn("friend", body)
        self.assertIn("重新扫码绑定", body)

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


if __name__ == "__main__":
    unittest.main()

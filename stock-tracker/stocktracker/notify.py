"""Notification channels. A channel is on when its required fields are filled in
(after ${ENV_VAR} expansion), so leaving a value empty simply disables it."""

from __future__ import annotations

import json
import logging
import os
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from typing import Any

import requests

from .alerts import Alert
from .config import expand_env

log = logging.getLogger("stocktracker")


class Notifier:
    name = "notifier"
    secrets: tuple[str, ...] = ()  # values hidden from error messages

    def send(self, alert: Alert) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, bell: bool = True, stream=None):
        self.bell = bell
        self.stream = stream or sys.stdout

    def send(self, alert: Alert) -> None:
        bar = "=" * min(78, max(40, len(alert.title) + 4))
        bell = "\a" if self.bell and getattr(self.stream, "isatty", lambda: False)() else ""
        indented = "\n".join("  " + line for line in alert.message.splitlines())
        print(f"{bell}\n{bar}\n  {alert.title}\n{indented}\n{bar}\n", file=self.stream, flush=True)


class NtfyNotifier(Notifier):
    """Push notifications to your phone/desktop through https://ntfy.sh (free, no account)."""

    name = "ntfy"
    _TAGS = {"in_stock": "white_check_mark", "target": "dart", "price_drop": "chart_with_downwards_trend"}

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None):
        self.topic, self.server, self.token = topic, server.rstrip("/"), token
        self.secrets = tuple(x for x in (token,) if x)

    def send(self, alert: Alert) -> None:
        payload: dict[str, Any] = {
            "topic": self.topic,
            "title": alert.title,
            "message": alert.message,
            "tags": [self._TAGS.get(k, "bell") for k in alert.kinds[:1]],
            "priority": 5 if "in_stock" in alert.kinds else 4,
        }
        if alert.url:
            payload["click"] = alert.url
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        requests.post(self.server, json=payload, headers=headers, timeout=15).raise_for_status()


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.secrets = (webhook_url,)

    def send(self, alert: Alert) -> None:
        content = f"**{alert.title}**\n{alert.message}"[:2000]
        requests.post(self.webhook_url, json={"content": content}, timeout=15).raise_for_status()


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token, self.chat_id = bot_token, chat_id
        self.secrets = (bot_token,)

    def send(self, alert: Alert) -> None:
        resp = requests.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={"chat_id": self.chat_id, "text": f"{alert.title}\n\n{alert.message}"[:4000]},
            timeout=15,
        )
        if resp.status_code >= 400:  # don't put the bot token into the error message
            raise RuntimeError(f"Telegram API returned HTTP {resp.status_code}: {resp.text[:200]}")


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, cfg: dict[str, Any]):
        self.host = str(cfg["smtp_host"])
        self.port = int(cfg.get("port") or 587)
        self.username = cfg.get("username") or ""
        self.password = cfg.get("password") or ""
        self.sender = cfg.get("from") or self.username
        to = cfg["to"]
        self.to = [t.strip() for t in to.split(",")] if isinstance(to, str) else list(to)
        self.starttls = cfg.get("starttls", True) is not False
        self.secrets = tuple(x for x in (self.password,) if x)

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = alert.title
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg.set_content(alert.message)
        if self.port == 465:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=20)
        with smtp:
            if self.port != 465 and self.starttls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)


class WebhookNotifier(Notifier):
    """POSTs JSON to any URL (Home Assistant, IFTTT, Zapier, n8n, your own script...)."""

    name = "webhook"

    def __init__(self, url: str):
        self.url = url
        self.secrets = (url,)

    def send(self, alert: Alert) -> None:
        body = {"title": alert.title, "message": alert.message, "url": alert.url, "kinds": alert.kinds, **alert.data}
        requests.post(self.url, json=body, timeout=15).raise_for_status()


class DesktopNotifier(Notifier):
    name = "desktop"

    def send(self, alert: Alert) -> None:
        title, message = alert.title[:120], alert.message[:400]
        try:
            from plyer import notification  # optional: pip install plyer

            notification.notify(title=title[:64], message=message[:256], timeout=15)
            return
        except ImportError:
            pass
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
            subprocess.run(["osascript", "-e", script], check=True, timeout=10)
        elif sys.platform.startswith("win"):
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                "$n.ShowBalloonTip(15000, $env:ST_TITLE, $env:ST_MESSAGE, 'Info'); "
                "Start-Sleep -Seconds 16; $n.Dispose()"
            )
            env = {**os.environ, "ST_TITLE": title, "ST_MESSAGE": message}
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, message], check=True, timeout=10)
        else:
            raise RuntimeError("no desktop notification tool found; run: pip install plyer")


def build_notifiers(cfg: dict[str, Any] | None, console_stream=None) -> list[Notifier]:
    cfg = expand_env(cfg or {})
    out: list[Notifier] = []
    if cfg.get("console", True) is not False:
        out.append(ConsoleNotifier(bell=cfg.get("bell", True) is not False, stream=console_stream))
    if cfg.get("desktop"):
        out.append(DesktopNotifier())
    ntfy = cfg.get("ntfy") or {}
    if ntfy.get("topic"):
        out.append(NtfyNotifier(str(ntfy["topic"]), str(ntfy.get("server") or "https://ntfy.sh"), ntfy.get("token") or None))
    discord = cfg.get("discord") or {}
    if discord.get("webhook_url"):
        out.append(DiscordNotifier(str(discord["webhook_url"])))
    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        out.append(TelegramNotifier(str(tg["bot_token"]), str(tg["chat_id"])))
    mail = cfg.get("email") or {}
    if mail.get("smtp_host") and mail.get("to"):
        out.append(EmailNotifier(mail))
    hook = cfg.get("webhook") or {}
    if hook.get("url"):
        out.append(WebhookNotifier(str(hook["url"])))
    return out


def send_all(notifiers: list[Notifier], alert: Alert) -> dict[str, str | None]:
    """Send to every channel; one failing channel never stops the others or the tracker."""
    results: dict[str, str | None] = {}
    for n in notifiers:
        try:
            n.send(alert)
            results[n.name] = None
        except Exception as e:  # noqa: BLE001
            msg = f"{e.__class__.__name__}: {e}"
            for secret in n.secrets:
                msg = msg.replace(secret, "***")
            log.warning("%s notification failed: %s", n.name, msg)
            results[n.name] = msg
    return results

"""Checking listings, recording history, sending alerts, and the continuous scheduler."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable

from .alerts import Alert, evaluate
from .config import Config, ConfigError, Listing
from .extract import ExtractError, Result, extract
from .fetch import BlockedError, Fetcher, FetchError
from .notify import Notifier, build_notifiers, send_all
from .storage import Store

log = logging.getLogger("stocktracker")


@dataclass
class Outcome:
    listing: Listing
    status: str  # ok | blocked | error
    result: Result | None = None
    error: str | None = None
    alert: Alert | None = None
    prev_price: float | None = None
    retry_after: float | None = None
    next_in: float | None = None  # set by the scheduler


class Tracker:
    def __init__(
        self,
        config: Config,
        store: Store,
        fetcher: Fetcher | None = None,
        notifiers: list[Notifier] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.store = store
        self.fetcher = fetcher or Fetcher(config.settings)
        self.notifiers = notifiers if notifiers is not None else build_notifiers(config.raw.get("notifications"))
        self.sleep = sleep
        self._mtime = self._config_mtime()
        # Scheduler state (read by the GUI): when each listing is due, consecutive failures,
        # and the listing being checked right now.
        self.due: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        self.current: Listing | None = None

    # ---- single checks

    def check(self, listing: Listing, send_alerts: bool = True) -> Outcome:
        prev = self.store.last_ok(listing.key)
        out = Outcome(listing=listing, status="ok", prev_price=prev["price"] if prev else None)
        try:
            out.result = extract(listing, self.fetcher)
        except BlockedError as e:
            out.status, out.error, out.retry_after = "blocked", str(e), e.retry_after
        except (FetchError, ExtractError) as e:
            out.status, out.error = "error", str(e)
        except Exception as e:  # noqa: BLE001 - a bad page must never kill the tracker
            log.debug("unexpected error checking %s", listing.label, exc_info=True)
            out.status, out.error = "error", f"unexpected {e.__class__.__name__}: {e}"

        if out.result is not None and not out.result.currency:
            out.result.currency = self.config.settings["currency"]
        self.store.record(listing, out.status, out.result, out.error)

        if out.status == "ok":
            out.alert = evaluate(listing, prev, out.result, self.config.settings)
            if out.alert:
                self.store.record_alert(listing, out.alert)
                if send_alerts:
                    send_all(self.notifiers, out.alert)
        return out

    def check_all(
        self,
        product: str | None = None,
        send_alerts: bool = True,
        on_outcome: Callable[[Outcome], None] | None = None,
    ) -> list[Outcome]:
        outcomes = []
        for listing in self.config.listings(product):
            out = self.check(listing, send_alerts=send_alerts)
            outcomes.append(out)
            if on_outcome:
                on_outcome(out)
        return outcomes

    # ---- continuous mode

    def _config_mtime(self) -> float | None:
        try:
            return self.config.path.stat().st_mtime
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Pick up edits to the config file while running (new products, intervals, ...)."""
        mtime = self._config_mtime()
        if mtime is None or mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            new = Config.load(self.config.path)
        except ConfigError as e:
            log.error("config changed but has errors, keeping the previous version:\n%s", e)
            return False
        self.config = new
        self.fetcher.configure(new.settings)
        self.notifiers = build_notifiers(new.raw.get("notifications"))
        for w in new.warnings:
            log.warning("%s", w)
        log.info("config reloaded: %d listing(s) to watch", len(new.listings()))
        return True

    def next_delay(self, out: Outcome, failures: int) -> float:
        settings = self.config.settings
        base = out.listing.interval
        if out.status == "ok":
            delay = base
        else:  # exponential backoff on errors / blocks
            delay = min(base * 2 ** min(failures, 10), max(settings["max_backoff"], base))
            if out.retry_after:
                delay = max(delay, out.retry_after)
        jitter = settings["jitter"]
        return max(1.0, delay * (1 + random.uniform(-jitter, jitter)))

    def _current_listings(self, product: str | None) -> dict[str, Listing]:
        try:
            return {l.key: l for l in self.config.listings(product)}
        except ConfigError as e:  # e.g. the filtered product was removed from the config
            log.error("%s", e)
            return {}

    def run_step(
        self,
        product: str | None = None,
        on_outcome: Callable[[Outcome], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        only: set[str] | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> float:
        """Check every listing that is due (optionally only `only`). Returns seconds until the next one."""
        self.reload_if_changed()
        listings = self._current_listings(product)
        now = clock()
        for key in list(self.due):
            if key not in listings:
                del self.due[key]
        for key in listings:
            self.due.setdefault(key, now)
        if not listings:
            return 5.0
        ready = sorted((k for k, t in self.due.items() if t <= now and (only is None or k in only)), key=self.due.get)
        for key in ready:
            if stop and stop():
                break
            self.current = listings[key]
            try:
                out = self.check(listings[key])
            finally:
                self.current = None
            self.failures[key] = 0 if out.status == "ok" else self.failures.get(key, 0) + 1
            out.next_in = self.next_delay(out, self.failures[key])
            self.due[key] = clock() + out.next_in
            if on_outcome:
                on_outcome(out)
        return max(0.0, min(self.due.values()) - clock())

    def run(
        self,
        product: str | None = None,
        on_outcome: Callable[[Outcome], None] | None = None,
        max_checks: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Check every listing on its own interval until interrupted (Ctrl+C)."""
        done = 0
        warned_empty = False

        def counted(out: Outcome) -> None:
            nonlocal done
            done += 1
            if on_outcome:
                on_outcome(out)

        def finished() -> bool:
            return max_checks is not None and done >= max_checks

        while not finished():
            wait = self.run_step(product, counted, clock, stop=finished)
            if not self.due:
                if not warned_empty:
                    log.warning("nothing to track yet; add products to %s (it is re-read automatically)", self.config.path)
                    warned_empty = True
            else:
                warned_empty = False
            if wait > 0 and not finished():
                self.sleep(min(wait, 5.0))  # wake regularly to notice config edits

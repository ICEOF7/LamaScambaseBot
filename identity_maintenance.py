"""Bounded, restart-safe draining of the ID queue. Stores counters, never secrets.

The complaints table is the queue; a restart must not reset candidates, decisions
or confirmed links. A short backlog delay is distinct from the idle refresh delay.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger('lamascambase.identity_jobs')


class IdentityMaintenance:
    def __init__(self, *, resolver, identities, settings, scan, refresh, lock,
                 wake, stop_event, idle_seconds=1800, batch_pause=10, clock=time.monotonic):
        self.resolver = resolver
        self.identities, self.settings = identities, settings
        self.scan, self.refresh, self.lock = scan, refresh, lock
        self.wake, self.stop_event = wake, stop_event
        self.idle_seconds = max(300.0, float(idle_seconds))
        self.batch_pause = max(5.0, float(batch_pause))
        self.clock = clock
        self.thread = None
        self._lifecycle = threading.Lock()
        self._not_before = 0.0
        self._errors = 0
        try:
            deadline = float(self.settings.setting('identity_retry_at', '0'))
            if math.isfinite(deadline) and deadline > time.time():
                self._not_before = self.clock() + max(0.0, deadline - time.time())
        except (ValueError, TypeError):
            pass

    def status(self):
        try:
            value = json.loads(self.settings.setting('identity_job_status', '{}'))
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _publish(self, state, **extra):
        data = {'state': state, 'updated_at': datetime.now(timezone.utc).isoformat(), **extra}
        self.settings.set_setting('identity_job_status', json.dumps(data))
        return data

    def _defer(self, delay, kind="error"):
        self._not_before = self.clock() + delay
        try:
            self.settings.set_setting('identity_retry_at', str(time.time() + delay))
            self.settings.set_setting('identity_retry_kind', kind)
        except Exception as exc:
            logger.warning('Cannot persist identity backoff (%s)', type(exc).__name__)

    @property
    def paused(self):
        return self.settings.setting('identity_scan_paused', '0') == '1'

    def pause(self):
        self.settings.set_setting('identity_scan_paused', '1')
        self._publish('pausing')
        self.wake.set()

    def resume(self):
        self.settings.set_setting('identity_scan_paused', '0')
        self.wake.set()

    def run_once(self):
        resolver = self.resolver()
        if not resolver.enabled:
            self._publish('disabled')
            return self.idle_seconds
        if self.paused:
            self._publish('paused')
            return self.idle_seconds
        remaining = self._not_before - self.clock()
        if remaining > 0:
            kind = self.settings.setting('identity_retry_kind', 'retry_wait')
            if kind not in {'error', 'rate_limited'}:
                kind = 'retry_wait'
            self._publish(kind, retry_after=math.ceil(remaining))
            return remaining
        if not self.lock.acquire(blocking=False):
            return self.batch_pause
        try:
            self._publish('running', remaining=self.identities.summary()['unscanned'])
            result = self.scan()
            remaining = self.identities.summary()['unscanned']
            if result.get('rate_limited'):
                delay = max(1.0, float(result.get('retry_after') or 60))
                self._defer(delay, 'rate_limited')
                self._publish('rate_limited', retry_after=int(delay), remaining=remaining)
                return delay
            if result.get('errors'):
                self._errors += 1
                delay = min(600.0, 60.0 * 2 ** min(self._errors - 1, 4))
                self._defer(delay)
                self._publish('error', error_code='resolver_error', retry_after=int(delay), remaining=remaining)
                return delay
            self._errors = 0
            self.settings.set_setting('identity_retry_at', '0')
            if not remaining and not self.stop_event.is_set() and not self.paused:
                self.refresh()
            if self.paused:
                state = 'paused'
            else:
                state = 'queued' if remaining else 'idle'
            self._publish(state, remaining=remaining, checked=result.get('targets', 0),
                          linked=result.get('linked', 0), candidates=result.get('candidates', 0),
                          unavailable=result.get('unavailable', 0))
            return self.batch_pause if remaining and not self.paused else self.idle_seconds
        finally:
            self.lock.release()

    def run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            try:
                delay = self.run_once()
            except Exception as exc:
                logger.error('Identity worker failure (%s)', type(exc).__name__)
                delay = 60.0
                self._defer(delay)
                try:
                    self._publish('error', error_code='worker_error', retry_after=60)
                except Exception:
                    pass
            if not self.stop_event.is_set():
                self.wake.wait(timeout=max(1.0, delay))

    def start(self):
        with self._lifecycle:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, name='telegram-identity-maintenance', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                logger.warning('Identity request has not finished during shutdown')

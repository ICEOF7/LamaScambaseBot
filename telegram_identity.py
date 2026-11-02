"""Optional Telegram MTProto resolver.

An HTTP Bot API token alone cannot reliably resolve arbitrary user @names.
Telegram's MTProto contacts.resolveUsername supports bots when an API ID/hash
are configured. The authenticated session is sensitive runtime data; it must
never be committed to a public repository.
"""

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger('lamascambase.identity')


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int
    username: str | None
    usernames: tuple[str, ...]
    checked_at: str


class IdentityRateLimited(RuntimeError):
    """Telegram has instructed the client to pause requests."""
    def __init__(self, message='Telegram resolution is cooling down', *, retry_after=60):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class TelegramResolver:
    def __init__(self, api_id: str, api_hash: str, bot_token: str,
                 session_path: str = 'data/telegram_identity', min_interval: float = 5.0):
        api_id, api_hash = str(api_id or '').strip(), str(api_hash or '').strip()
        self.api_id = int(api_id) if api_id.isascii() and api_id.isdigit() else 0
        self.api_hash = api_hash
        self.enabled = bool(self.api_id > 0 and api_hash and bot_token)
        self.bot_token = bot_token
        self.session_path = str(session_path)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._cooldown_until = 0.0
        self.min_interval = max(0.0, float(min_interval))

    @staticmethod
    def _extract_user(entity):
        from telethon.tl.types import User

        if not isinstance(entity, User) or not entity.id:
            return None
        username = getattr(entity, 'username', None)
        names = []
        for value in [username] + [getattr(item, 'username', None) for item in
                                   (getattr(entity, 'usernames', None) or [])
                                   if getattr(item, 'active', True)]:
            name = '@' + str(value).lstrip('@').lower() if value else None
            if name and name not in names:
                names.append(name)
        return TelegramIdentity(int(entity.id), '@' + username.lower() if username else None,
                                tuple(names), datetime.now(timezone.utc).isoformat())

    async def _fetch(self, reference: str | int):
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError
        from telethon.tl.types import PeerUser
        from telethon.tl.functions.contacts import ResolveUsernameRequest

        Path(self.session_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(self.session_path, self.api_id, self.api_hash,
                                receive_updates=False, flood_sleep_threshold=0,
                                request_retries=1, connection_retries=1)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(bot_token=self.bot_token)
            me = await client.get_me()
            expected_bot_id = int(self.bot_token.split(':', 1)[0])
            if not getattr(me, 'bot', False) or int(me.id) != expected_bot_id:
                raise RuntimeError('MTProto session belongs to another account; use a separate session path')
            if isinstance(reference, str):
                try:
                    resolved = await client(ResolveUsernameRequest(reference.lstrip('@')))
                except (UsernameNotOccupiedError, UsernameInvalidError):
                    return None
                peer_id = getattr(resolved.peer, 'user_id', None)
                entity = next((u for u in resolved.users if u.id == peer_id), None)
            else:
                try:
                    entity = await client.get_entity(PeerUser(reference))
                except ValueError:
                    return None
            identity = self._extract_user(entity)
            if identity and isinstance(reference, str):
                if reference.lower() not in identity.usernames:
                    return None
            return identity
        except FloodWaitError as exc:
            seconds = max(1, int(exc.seconds))
            self._cooldown_until = time.monotonic() + seconds
            raise IdentityRateLimited(retry_after=seconds) from exc
        finally:
            await client.disconnect()

    def _resolve(self, reference: str | int):
        if not self.enabled:
            return None
        with self._lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                raise IdentityRateLimited(retry_after=math.ceil(self._cooldown_until - now))
            gap = self.min_interval - (now - self._last_request)
            if self._last_request and gap > 0:
                time.sleep(gap)
            self._last_request = time.monotonic()
            return asyncio.run(asyncio.wait_for(self._fetch(reference), timeout=20))

    def resolve_username(self, username: str):
        if not isinstance(username, str) or not username.startswith('@'):
            raise ValueError('Expected an @username')
        return self._resolve(username.lower())

    def resolve_id(self, user_id: int):
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError('Expected a positive Telegram user ID')
        return self._resolve(user_id)

"""Первый вход в аккаунт: `make login`.

Код приходит в Telegram, потом пароль 2FA (TZ §4.1). Печатает StringSession —
её кладут в секреты Amvera как TG_SESSION_STRING (TZ шаг 13).

Строка сессии — полный доступ к аккаунту. Никогда не коммитить и не слать в чат.
"""
from __future__ import annotations

import asyncio

from telethon.sessions import StringSession

from src.collector.client import build_client, harden_session_file
from src.config import cfg


async def _main() -> None:
    client = build_client()
    await client.start()  # Telethon сам спросит телефон, код и пароль 2FA
    me = await client.get_me()
    print(f"\nГотово. Вошли как {getattr(me, 'username', None) or me.id} (id={me.id})")

    if not cfg.TG_SESSION_STRING:
        harden_session_file()
        print(f"Файл сессии: {cfg.TG_SESSION} (права 600, в git не попадёт)")
        print("\nСтрока сессии для деплоя (TG_SESSION_STRING), никому не показывай:\n")
        print(StringSession.save(client.session))

    print("\nДиалоги аккаунта (id группы для TG_GROUP_ID):")
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            print(f"  {dialog.id:>16}  {dialog.name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())

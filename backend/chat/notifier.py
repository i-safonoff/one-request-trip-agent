import asyncio


class Notifier:
    """In-process wake-up for the Telegram delivery loop, so replies don't wait for the next poll tick."""

    def __init__(self):
        self._event = asyncio.Event()

    def poke(self) -> None:
        self._event.set()

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._event.clear()


notifier = Notifier()

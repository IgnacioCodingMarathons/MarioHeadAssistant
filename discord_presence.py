import time
from pathlib import Path


class DiscordPresence:
    def __init__(self):
        self.client = None
        self.enabled = False
        self.started = None
        self.game = None

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if not enabled:
            self.clear()
            return
        try:
            client_id = (Path(__file__).resolve().parent / 'assets' / 'discord.richpresence') \
                .read_text(encoding='utf-8').strip()
        except OSError:
            client_id = ''
        if not client_id:
            print('MarioAssistant Discord: set MARIOASSISTANT_DISCORD_CLIENT_ID to enable Rich Presence')
            self.enabled = False
            return
        try:
            from pypresence import Presence
            self.client = Presence(client_id)
            self.client.connect()
            self.update_idle()
        except Exception as error:
            print(f'MarioAssistant Discord: {type(error).__name__}: {error}')
            self.client = None
            self.enabled = False

    def update_idle(self):
        self.game = None
        self.started = None
        self._update('Idling', '')

    def update_game(self, name, stats):
        if self.game != name or self.started is None:
            self.game = name
            self.started = int(time.time())
        self._update(f'Playing {name}', stats)

    def _update(self, details, state):
        if not self.enabled or self.client is None:
            return
        try:
            self.client.update(details=details, state=state, start=self.started)
        except Exception as error:
            print(f'MarioAssistant Discord: update failed: {type(error).__name__}: {error}')

    def clear(self):
        if self.client is not None:
            try:
                self.client.clear()
                self.client.close()
            except Exception:
                pass
        self.client = None
        self.game = None
        self.started = None

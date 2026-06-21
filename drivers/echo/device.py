"""Echo device — capabilities, capability listeners, and pushed state updates."""

from typing import TYPE_CHECKING, cast

from homey import device

if TYPE_CHECKING:
    from ...app import App

AUDIO_CAPABILITIES = (
    "speaker_playing",
    "speaker_prev",
    "speaker_next",
    "speaker_track",
    "speaker_album",
    "speaker_artist",
)
# aioamazondevices cannot SET shuffle/repeat, so these are kept read-only.
READONLY_CAPABILITIES = ("speaker_shuffle", "speaker_repeat")


class EchoDevice(device.Device):
    @property
    def serial(self) -> str:
        return self.get_data()["id"]

    @property
    def _alexa(self):
        return cast("App", self.homey.app).alexa

    async def on_init(self) -> None:
        store = self.get_store()
        capabilities = store.get("capabilities", []) or []
        model = store.get("model", {}) or {}

        await self.set_settings(
            {
                "serial_number": self.serial,
                "model_number": model.get("id"),
                "capabilities": ", ".join(capabilities),
            }
        )

        if "AUDIO_CONTROLS" in capabilities:
            for capability in AUDIO_CAPABILITIES:
                await self.add_capability(capability)
            for capability in READONLY_CAPABILITIES:
                if not self.has_capability(capability):
                    await self.add_capability(capability)
                await self.set_capability_options(capability, {"setable": False})

        if "VOLUME_SETTING" in capabilities:
            await self.add_capability("volume_set")
            if self.has_capability("volume_set.notifications"):
                await self.remove_capability("volume_set.notifications")

        self.register_capability_listener("volume_set", self._on_volume_set)
        self.register_capability_listener("speaker_playing", self._on_playing)
        self.register_capability_listener("speaker_next", self._on_next)
        self.register_capability_listener("speaker_prev", self._on_prev)

        self._album_art = await self.homey.images.create_image()
        await self.set_album_art_image(self._album_art)

        if self._alexa.state != "connected":
            await self.set_unavailable("Not connected to Amazon")

        self.log(f"{self.get_name()} with id {self.serial} has been initialized")

    # --- capability listeners (Homey -> Alexa) ---------------------------
    async def _on_volume_set(self, value: float, **kwargs) -> None:
        await self._alexa.set_volume(self.serial, value)

    async def _on_playing(self, value: bool, **kwargs) -> None:
        await self._alexa.playback(self.serial, "play" if value else "pause")

    async def _on_next(self, value=None, **kwargs) -> None:
        await self._alexa.playback(self.serial, "next")

    async def _on_prev(self, value=None, **kwargs) -> None:
        await self._alexa.playback(self.serial, "previous")

    # --- pushed updates (Alexa -> Homey) ---------------------------------
    async def apply_volume(self, value: float) -> None:
        if self.has_capability("volume_set"):
            await self.set_capability_value("volume_set", value)

    async def apply_media(self, media) -> None:
        # The now-playing endpoint flaps to player_state=None (no data) between real
        # updates; ignore those so we don't clobber a known PLAYING/PAUSED state.
        if media.player_state is None:
            return
        if self.has_capability("speaker_playing"):
            await self.set_capability_value("speaker_playing", media.player_state == "PLAYING")
        if self.has_capability("speaker_track"):
            await self.set_capability_value("speaker_track", media.now_playing_title or "")
            await self.set_capability_value("speaker_artist", media.now_playing_line1 or "")
            await self.set_capability_value("speaker_album", media.now_playing_line2 or "")

        # Homey's Image.set_url() has no None guard (it calls url.startswith), so only
        # set the art when we actually have a valid https URL.
        url = media.now_playing_url
        if url and url.startswith("https://"):
            self._album_art.set_url(url)
            await self._album_art.update()


homey_export = EchoDevice

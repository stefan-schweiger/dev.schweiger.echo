"""Echo device — capabilities, capability listeners, and pushed state updates."""

from typing import TYPE_CHECKING, cast

from homey import device

if TYPE_CHECKING:
    from ...app import App
    from .driver import EchoDriver

DND_CAPABILITY = "do_not_disturb"
SCREEN_CAPABILITY = "onoff.display"

BRIGHTNESS_SCALE = 100

# Screen controls, only for devices whose Amazon capability list advertises them
# (Echo Show / Spot / Dot with clock). Each maps to a per-device setting behind
# /api/v1/devices/<deviceAccountId>/settings/<name> — see AlexaService.
# Homey capability -> (Amazon capability, setting name, decode, encode)
SCREEN_SETTINGS = {
    SCREEN_CAPABILITY: (
        "DISPLAY_POWER_TOGGLE",
        "displayPower",
        lambda raw: raw == "ON",
        lambda value: "ON" if value else "OFF",
    ),
    "dim": (
        "DISPLAY_BRIGHTNESS_ADJUST",
        "brightness",
        lambda raw: float(raw) / BRIGHTNESS_SCALE,
        lambda value: round(value * BRIGHTNESS_SCALE),
    ),
    "adaptive_brightness": (
        "DISPLAY_ADAPTIVE_BRIGHTNESS",
        "adaptiveBrightness",
        lambda raw: raw == "ON",
        lambda value: "ON" if value else "OFF",
    ),
}

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

        # Amazon exposes no per-device flag for Do Not Disturb — every Echo
        # supports it — so it's added unconditionally. Devices Amazon doesn't
        # report DND for simply never get a value (see App._on_dnd).
        if not self.has_capability(DND_CAPABILITY):
            await self.add_capability(DND_CAPABILITY)

        for capability, (amazon_capability, _, _, _) in SCREEN_SETTINGS.items():
            if amazon_capability not in capabilities:
                # Not a screen device (or Amazon dropped the capability) — make
                # sure a stale control doesn't linger after a device swap.
                if self.has_capability(capability):
                    await self.remove_capability(capability)
                continue
            if not self.has_capability(capability):
                await self.add_capability(capability)
            self.register_capability_listener(capability, self._screen_listener(capability))

        self.register_capability_listener("volume_set", self._on_volume_set)
        self.register_capability_listener("speaker_playing", self._on_playing)
        self.register_capability_listener("speaker_next", self._on_next)
        self.register_capability_listener("speaker_prev", self._on_prev)
        self.register_capability_listener(DND_CAPABILITY, self._on_dnd_set)

        self._album_art = await self.homey.images.create_image()
        await self.set_album_art_image(self._album_art)

        if self._alexa.state != "connected":
            await self.set_unavailable("Not connected to Amazon")
        else:
            # Covers a freshly paired device; at app start the connection isn't
            # up yet and App._auto_connect does this instead.
            await self.refresh_screen_state()

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

    async def _on_dnd_set(self, value: bool, **kwargs) -> None:
        await self._alexa.set_do_not_disturb(self.serial, value)
        # Homey stores the new value itself once this listener resolves, but the
        # Flow triggers are ours to fire (custom capabilities get no automatic
        # cards), so route it through the same path as a polled change.
        await self.apply_dnd(value)

    def _screen_listener(self, capability: str):
        # One listener per screen capability — Homey doesn't tell a listener
        # which capability it fired for.
        async def listener(value, **kwargs) -> None:
            _, setting, _, encode = SCREEN_SETTINGS[capability]
            await self._alexa.set_device_setting(self.serial, setting, encode(value))
            await self._apply_screen_value(capability, value)

        return listener

    # --- flow-card entry points (driver -> device) -----------------------
    async def set_screen(self, on: bool) -> None:
        await self.trigger_capability_listener(SCREEN_CAPABILITY, on)

    def is_screen_on(self) -> bool:
        return bool(self.get_capability_value(SCREEN_CAPABILITY))

    async def set_adaptive_brightness(self, on: bool) -> None:
        await self.trigger_capability_listener("adaptive_brightness", on)

    def is_adaptive_brightness_on(self) -> bool:
        return bool(self.get_capability_value("adaptive_brightness"))

    async def set_dnd(self, enabled: bool) -> None:
        # Via the capability listener so Amazon, Homey's stored value and the
        # Flow triggers all stay in step.
        await self.trigger_capability_listener(DND_CAPABILITY, enabled)

    def is_dnd_on(self) -> bool:
        return bool(self.get_capability_value(DND_CAPABILITY))

    # --- pushed updates (Alexa -> Homey) ---------------------------------
    async def apply_volume(self, value: float) -> None:
        if self.has_capability("volume_set"):
            await self.set_capability_value("volume_set", value)

    async def apply_dnd(self, enabled: bool) -> None:
        if not self.has_capability(DND_CAPABILITY):
            return
        previous = self.get_capability_value(DND_CAPABILITY)
        if previous == enabled:
            return
        await self.set_capability_value(DND_CAPABILITY, enabled)
        # previous is None the first time we ever learn this device's DND state
        # (freshly added capability). That's not a change the user made, so it
        # must not fire the Flow trigger.
        if previous is not None:
            await cast("EchoDriver", self.driver).trigger_dnd(self, enabled)

    async def refresh_screen_state(self) -> None:
        """Poll the display settings — Amazon pushes no directive for these.

        One GET per setting per device, so it only runs for devices that
        actually have a screen. Best-effort: a device that refuses (or a
        missing deviceAccountId) must not take down the sync that called us.
        """
        for capability, (_, setting, decode, _encode) in SCREEN_SETTINGS.items():
            if not self.has_capability(capability):
                continue
            try:
                raw = await self._alexa.get_device_setting(self.serial, setting)
                if raw is not None:
                    await self._apply_screen_value(capability, decode(raw))
            except Exception as e:  # noqa: BLE001
                self.error(f"Could not read {setting}: {type(e).__name__}: {e}")

    async def _apply_screen_value(self, capability: str, value) -> None:
        previous = self.get_capability_value(capability)
        if previous == value:
            return
        await self.set_capability_value(capability, value)
        # `dim` is a system capability and gets its Flow cards for free;
        # onoff.display is a sub-capability, which Homey does not generate cards
        # for, so its triggers are ours to fire. `previous is None` means we're
        # just learning the state, not observing a change.
        if capability == SCREEN_CAPABILITY and previous is not None:
            await cast("EchoDriver", self.driver).trigger_screen(self, bool(value))

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

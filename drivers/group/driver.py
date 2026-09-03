"""Speaker-group driver — pairing only (filters multi-room / WHA devices).

Deliberately registers no Flow cards. A `WHA` group is only a media target for
Amazon: playback, volume and now-playing work on it, but Speak/Announce, `Say
with Voice`, `textCommand`, sounds, routines and DND all need an individual
device serial and are rejected for a group serial. Every card therefore lives on
the echo driver, scoped `driver_id=echo`. See "Known limitations" in AGENTS.md
before adding cards here.
"""

from typing import TYPE_CHECKING, cast

from homey import driver

if TYPE_CHECKING:
    from ...app import App


class GroupDriver(driver.Driver):
    @property
    def _alexa(self):
        return cast("App", self.homey.app).alexa

    async def on_init(self) -> None:
        self.log("GroupDriver has been initialized")

    async def on_pair_list_devices(self, view_data=None) -> list[dict]:
        app = cast("App", self.homey.app)
        app.reset_pairing_reconnect()
        if not await app.ensure_amazon_connected():
            self.error("Pairing: not connected to Amazon — sign in via app settings first")
            return []
        devices = await self._alexa.pairing_devices("group")
        self.log(f"Pairing: {len(devices)} group device(s) found")
        return devices


homey_export = GroupDriver
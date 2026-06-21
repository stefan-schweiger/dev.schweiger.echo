"""Speaker-group driver — pairing only (filters multi-room / WHA devices)."""

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
        return await self._alexa.pairing_devices("group")


homey_export = GroupDriver

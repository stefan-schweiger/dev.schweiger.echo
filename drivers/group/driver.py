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

    async def on_pair(self, session) -> None:
        # Defining on_pair stops the list_devices template from auto-wiring to
        # on_pair_list_devices, so register both handlers explicitly. The
        # connection_check view emits 'check_connection' and only proceeds when truthy.
        async def check_connection(data=None) -> bool:
            return self._alexa.state == "connected"

        async def list_devices(data=None) -> list[dict]:
            return await self.on_pair_list_devices()

        session.set_handler("check_connection", check_connection)
        session.set_handler("list_devices", list_devices)

    async def on_pair_list_devices(self, view_data=None) -> list[dict]:
        return await self._alexa.pairing_devices("group")


homey_export = GroupDriver

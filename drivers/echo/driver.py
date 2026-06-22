"""Echo driver — pairing (single devices) and flow action registration."""

from typing import TYPE_CHECKING, Any, Mapping, cast

from homey import driver

if TYPE_CHECKING:
    from ...app import App


def _serial(card_arguments: Mapping[str, Any]) -> str:
    return card_arguments["device"].get_data()["id"]


def _speech_mode(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("id", value.get("name", "speak")))
    return "speak"


class EchoDriver(driver.Driver):
    @property
    def _alexa(self):
        return cast("App", self.homey.app).alexa

    async def on_init(self) -> None:
        flow = self.homey.flow

        async def on_message(args: Mapping[str, Any], **kwargs) -> None:
            mode = _speech_mode(args["speech"])
            self.log(f"Flow message: mode={mode!r}")
            await self._alexa.say(_serial(args), args["message"], mode)

        async def autocomplete_voice(query: str, **kwargs) -> list[dict]:
            return [{"name": v["name"], "data": {"id": v["id"]}} for v in self._alexa.list_voices(query)]

        async def on_message_with_voice(args: Mapping[str, Any], **kwargs) -> None:
            await self._alexa.say_with_voice(
                _serial(args),
                args["message"],
                args["voice"]["data"]["id"],
                _speech_mode(args["speech"]),
            )

        async def on_command(args: Mapping[str, Any], **kwargs) -> None:
            await self._alexa.execute_command(_serial(args), args["command"])

        async def autocomplete_sound(query: str, **kwargs) -> list[dict]:
            q = (query or "").lower()
            return [
                {"name": s["name"], "data": {"id": s["id"]}}
                for s in self._alexa.list_sounds()
                if q in s["name"].lower()
            ]

        async def on_sound(args: Mapping[str, Any], **kwargs) -> None:
            await self._alexa.play_sound(_serial(args), args["sound"]["data"]["id"])

        async def autocomplete_routine(query: str, **kwargs) -> list[dict]:
            q = (query or "").lower()
            return [
                {"name": name, "data": {"name": name}}
                for name in await self._alexa.list_routines()
                if q in name.lower()
            ]

        async def on_routine(args: Mapping[str, Any], **kwargs) -> None:
            await self._alexa.run_routine(args["routine"]["data"]["name"])

        flow.get_action_card("message").register_run_listener(on_message)
        voice = flow.get_action_card("message_with_voice")
        voice.register_argument_autocomplete_listener("voice", autocomplete_voice)
        voice.register_run_listener(on_message_with_voice)
        flow.get_action_card("command").register_run_listener(on_command)
        sound = flow.get_action_card("play-sound")
        sound.register_argument_autocomplete_listener("sound", autocomplete_sound)
        sound.register_run_listener(on_sound)
        routine = flow.get_action_card("run-routine")
        routine.register_argument_autocomplete_listener("routine", autocomplete_routine)
        routine.register_run_listener(on_routine)

        self.log("EchoDriver has been initialized")

    async def on_pair_list_devices(self, view_data=None) -> list[dict]:
        app = cast("App", self.homey.app)
        app.reset_pairing_reconnect()
        if not await app.ensure_amazon_connected():
            self.error("Pairing: not connected to Amazon — sign in via app settings first")
            return []
        devices = await self._alexa.pairing_devices("echo")
        self.log(f"Pairing: {len(devices)} echo device(s) found")
        return devices


homey_export = EchoDriver
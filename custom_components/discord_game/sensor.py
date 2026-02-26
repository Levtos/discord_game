import asyncio
import logging
import re

import homeassistant.helpers.config_validation as cv
import validators
import voluptuous as vol
from homeassistant import config_entries, core
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import CONF_ACCESS_TOKEN, EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.entity import DeviceInfo
from nextcord import ActivityType, Member, RawReactionActionEvent, User, VoiceState
from nextcord.abc import GuildChannel

from .const import CONF_CHANNELS, CONF_IMAGE_FORMAT, CONF_MEMBERS, DOMAIN

_LOGGER = logging.getLogger(__name__)

ENTITY_ID_FORMAT = "sensor.discord_user_{}"
ENTITY_ID_CHANNEL_FORMAT = "sensor.discord_channel_{}"

# Keep only the explicitly requested entity attributes.
SENSORS = ["avatar_url", "game", "user_name"]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        vol.Required(CONF_MEMBERS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(CONF_CHANNELS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_IMAGE_FORMAT, default="webp"): vol.In(["png", "webp", "jpeg", "jpg"]),
    }
)


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
) -> None:
    """Setup sensors from a config entry created in the integrations UI."""
    config = hass.data[DOMAIN][config_entry.entry_id]
    import nextcord

    token = config.get(CONF_ACCESS_TOKEN)
    image_format = config.get(CONF_IMAGE_FORMAT)

    bot = nextcord.Client(loop=hass.loop, intents=nextcord.Intents.all())
    await bot.login(token)

    async def async_stop_server(event):
        await bot.close()

    def task_callback(task: asyncio.Task):
        """Log background task failures to avoid silent crashes."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOGGER.exception("Discord client task failed", exc_info=exc)

    async def start_server(event):
        _LOGGER.debug("Starting server")
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop_server)
        task = asyncio.create_task(bot.start(token))
        task.add_done_callback(task_callback)

    hass.bus.async_listen_once("discord_game_setup_finished", start_server)

    @bot.event
    async def on_error(event_name, *args, **kwargs):
        _LOGGER.exception("Discord event handler failed: %s", event_name)

    async def update_discord_entity(_watcher: "DiscordAsyncMemberState", discord_member: Member):
        _watcher._state = str(discord_member.status)
        _watcher.display_name = discord_member.display_name
        _watcher.game = None

        for activity in discord_member.activities:
            if activity.type == ActivityType.playing:
                _watcher.game = activity.name
                break

        if _watcher.hass is not None:
            _watcher.async_schedule_update_ha_state(False)

    async def update_discord_entity_user(_watcher: "DiscordAsyncMemberState", discord_user: User):
        _watcher.avatar_url = str(
            discord_user.display_avatar.with_size(1024).with_static_format(image_format)
        )
        _watcher.userid = discord_user.id
        _watcher.member = discord_user.name
        _watcher.user_name = discord_user.global_name or discord_user.name
        if _watcher.hass is not None:
            _watcher.async_schedule_update_ha_state(False)

    @bot.event
    async def on_ready():
        users = {str(_user.id): _user for _user in bot.users}
        members = {str(_member.id): _member for _member in list(bot.get_all_members())}
        for _watcher in watchers.values():
            user = users.get(str(_watcher.userid))
            member = members.get(str(_watcher.userid))
            if user is not None:
                await update_discord_entity_user(_watcher, user)
            if member is not None:
                await update_discord_entity(_watcher, member)
            else:
                _watcher._state = "offline"
                if _watcher.hass is not None:
                    _watcher.async_schedule_update_ha_state(False)

            for sensor in _watcher.sensors.values():
                if sensor.hass is not None:
                    sensor.async_schedule_update_ha_state(False)

        for _chan in channels.values():
            if _chan.hass is not None:
                _chan.async_schedule_update_ha_state(False)

    @bot.event
    async def on_member_update(before: Member, after: Member):
        _watcher = watchers.get(str(after.id))
        if _watcher is not None:
            await update_discord_entity(_watcher, after)
            for sensor in _watcher.sensors.values():
                if sensor.hass is not None:
                    sensor.async_schedule_update_ha_state(False)

    @bot.event
    async def on_presence_update(before: Member, after: Member):
        _watcher = watchers.get(str(after.id))
        if _watcher is not None:
            await update_discord_entity(_watcher, after)
            for sensor in _watcher.sensors.values():
                if sensor.hass is not None:
                    sensor.async_schedule_update_ha_state(False)

    @bot.event
    async def on_user_update(before: User, after: User):
        _watcher: DiscordAsyncMemberState = watchers.get(str(after.id))
        if _watcher is not None:
            await update_discord_entity_user(_watcher, after)
            for sensor in _watcher.sensors.values():
                if sensor.hass is not None:
                    sensor.async_schedule_update_ha_state(False)

    @bot.event
    async def on_voice_state_update(_member: Member, before: VoiceState, after: VoiceState):
        # Keep state updates flowing for current architecture.
        _watcher = watchers.get(str(_member.id))
        if _watcher is not None:
            if after.channel is None and _watcher._state == "online":
                if _watcher.hass is not None:
                    _watcher.async_schedule_update_ha_state(False)
                for sensor in _watcher.sensors.values():
                    if sensor.hass is not None:
                        sensor.async_schedule_update_ha_state(False)

    @bot.event
    async def on_raw_reaction_add(payload: RawReactionActionEvent):
        _chan = channels.get(str(payload.channel_id))
        member: Member | None = payload.member
        if _chan and member is not None:
            _chan._state = member.display_name
            _chan._last_user = member.display_name
            if _chan.hass is not None:
                _chan.async_schedule_update_ha_state(False)

    watchers = {}
    for member in config.get(CONF_MEMBERS):
        if re.match(r"^\d{1,20}$", str(member)):
            user = await bot.fetch_user(member)
            if user:
                watcher = DiscordAsyncMemberState(hass, bot, user.name, user.global_name, user.id)
                watchers[str(watcher.userid)] = watcher

    channels = {}
    for channel in config.get(CONF_CHANNELS):
        if re.match(r"^\d{1,20}$", str(channel)):
            chan: GuildChannel = await bot.fetch_channel(channel)
            if chan:
                ch = DiscordAsyncReactionState(hass, bot, chan.name, chan.id)
                channels[str(chan.id)] = ch

    if watchers or channels:
        if watchers:
            async_add_entities(watchers.values())
            for sensors in watchers.values():
                async_add_entities(sensors.sensors.values())
        if channels:
            async_add_entities(channels.values())
        hass.bus.async_fire("discord_game_setup_finished")


class DiscordAsyncMemberState(SensorEntity):
    def __init__(self, hass, client, member, user_name, userid):
        self.member = member
        self.userid = userid
        self.hass = hass
        self.client = client
        self._state = "unknown"
        self.user_name = user_name or member
        self.display_name = None
        self.game = None
        self.avatar_url = None
        self.entity_id = ENTITY_ID_FORMAT.format(self.userid)
        self.sensors = {sensor_name: GenericSensor(sensor=self, attr=sensor_name) for sensor_name in SENSORS}

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, str(self.userid))}, name=self.member)

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def unique_id(self):
        return ENTITY_ID_FORMAT.format(self.userid)

    @property
    def name(self):
        return self.member

    @property
    def entity_picture(self):
        return self.avatar_url

    @property
    def extra_state_attributes(self):
        return {
            "user_id": self.userid,
            "user_name": self.user_name,
            "display_name": self.display_name,
            "game": self.game,
            "avatar_url": self.avatar_url,
        }


class GenericSensor(SensorEntity):
    def __init__(self, sensor: DiscordAsyncMemberState, attr: str):
        self.sensor = sensor
        self.attr = attr
        self.entity_id = ENTITY_ID_FORMAT.format(self.sensor.userid) + "_" + self.attr

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str:
        return getattr(self.sensor, self.attr)

    @property
    def unique_id(self):
        return ENTITY_ID_FORMAT.format(self.sensor.userid) + "_" + self.attr

    @property
    def name(self):
        return self.sensor.member + " " + self.attr

    @property
    def entity_picture(self):
        attr = getattr(self.sensor, self.attr)
        if validators.url(attr):
            return attr
        return None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, str(self.sensor.userid))}, name=self.sensor.member)


class DiscordAsyncReactionState(SensorEntity):
    def __init__(self, hass, client, channel, channelid):
        self._channel_name = channel
        self._channel_id = channelid
        self._hass = hass
        self._client = client
        self._state = "unknown"
        self._last_user = None
        self.entity_id = ENTITY_ID_CHANNEL_FORMAT.format(self._channel_id)

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def unique_id(self):
        return ENTITY_ID_CHANNEL_FORMAT.format(self._channel_id)

    @property
    def name(self):
        return self._channel_name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self.unique_id)}, name=self._channel_name)

    @property
    def extra_state_attributes(self):
        return {"last_user": self._last_user}

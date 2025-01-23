"""Representation of OCPP Entities."""

from __future__ import annotations

import logging
import ssl
from functools import partial
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OK
from homeassistant.core import HomeAssistant
from websockets import Subprotocol, NegotiationError
import websockets.server
from websockets.asyncio.server import ServerConnection

from .chargepoint import CentralSystemSettings
from .ocppv16 import ChargePoint as ChargePointv16
from .ocppv201 import ChargePoint as ChargePointv201

from .const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_SUBPROTOCOL,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    DEFAULT_CPID,
    DEFAULT_CSID,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SSL,
    DEFAULT_SSL_CERTFILE_PATH,
    DEFAULT_SSL_KEYFILE_PATH,
    DEFAULT_SUBPROTOCOL,
    DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_INTERVAL,
    DEFAULT_WEBSOCKET_PING_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_TRIES,
    DOMAIN,
    OCPP_2_0,
)
from .enums import (
    HAChargerServices as csvcs,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Instantiate instance of a CentralSystem."""
        self.hass = hass
        self.entry = entry
        self.host = entry.data.get(CONF_HOST, DEFAULT_HOST)
        self.port = entry.data.get(CONF_PORT, DEFAULT_PORT)

        self.settings = CentralSystemSettings()
        self.settings.csid = entry.data.get(CONF_CSID, DEFAULT_CSID)
        self.settings.cpid = entry.data.get(CONF_CPID, DEFAULT_CPID)

        self.settings.websocket_close_timeout = entry.data.get(
            CONF_WEBSOCKET_CLOSE_TIMEOUT, DEFAULT_WEBSOCKET_CLOSE_TIMEOUT
        )
        self.settings.websocket_ping_tries = entry.data.get(
            CONF_WEBSOCKET_PING_TRIES, DEFAULT_WEBSOCKET_PING_TRIES
        )
        self.settings.websocket_ping_interval = entry.data.get(
            CONF_WEBSOCKET_PING_INTERVAL, DEFAULT_WEBSOCKET_PING_INTERVAL
        )
        self.settings.websocket_ping_timeout = entry.data.get(
            CONF_WEBSOCKET_PING_TIMEOUT, DEFAULT_WEBSOCKET_PING_TIMEOUT
        )
        self.settings.config = entry.data

        self.subprotocols: list[Subprotocol] = entry.data.get(
            CONF_SUBPROTOCOL, DEFAULT_SUBPROTOCOL
        ).split(",")
        self._server = None
        self.config = entry.data
        self.id = entry.entry_id
        self.charge_points = {}

    @staticmethod
    async def create(hass: HomeAssistant, entry: ConfigEntry):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem(hass, entry)

        if self.entry.data.get(CONF_SSL, DEFAULT_SSL):
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            localhost_certfile = self.entry.data.get(
                CONF_SSL_CERTFILE_PATH, DEFAULT_SSL_CERTFILE_PATH
            )
            localhost_keyfile = self.entry.data.get(
                CONF_SSL_KEYFILE_PATH, DEFAULT_SSL_KEYFILE_PATH
            )
            await self.hass.async_add_executor_job(
                partial(
                    self.ssl_context.load_cert_chain,
                    localhost_certfile,
                    keyfile=localhost_keyfile,
                )
            )
        else:
            self.ssl_context = None

        server = await websockets.serve(
            self.on_connect,
            self.host,
            self.port,
            select_subprotocol=self.select_subprotocol,
            subprotocols=self.subprotocols,
            ping_interval=None,  # ping interval is not used here, because we send pings manually in ChargePoint.monitor_connection()
            ping_timeout=None,
            close_timeout=self.settings.websocket_close_timeout,
            ssl=self.ssl_context,
        )
        self._server = server
        return self

    def select_subprotocol(
        self, connection: ServerConnection, subprotocols
    ) -> Subprotocol | None:
        """Override default subprotocol selection."""

        if not subprotocols:
            return None

        proposed_subprotocols = set(subprotocols)
        for subprotocol in proposed_subprotocols:
            if subprotocol in self.subprotocols:
                return subprotocol

        raise NegotiationError(
            "invalid subprotocol; expected one of " + ", ".join(self.subprotocols)
        )

    async def on_connect(self, websocket: ServerConnection):
        """Request handler executed for every new OCPP connection."""
        if websocket.subprotocol is not None:
            _LOGGER.info("Websocket Subprotocol matched: %s", websocket.subprotocol)
        else:
            _LOGGER.info(
                "Websocket Subprotocol not provided by charger: default to ocpp1.6"
            )

        _LOGGER.info(f"Charger websocket path={websocket.request.path}")
        cp_id = websocket.request.path.strip("/")
        cp_id = cp_id[cp_id.rfind("/") + 1 :]
        if self.settings.cpid not in self.charge_points:
            _LOGGER.info(f"Charger {cp_id} connected to {self.host}:{self.port}.")
            if websocket.subprotocol and websocket.subprotocol.startswith(OCPP_2_0):
                charge_point = ChargePointv201(
                    cp_id, websocket, self.hass, self.entry, self.settings
                )
            else:
                charge_point = ChargePointv16(
                    cp_id, websocket, self.hass, self.entry, self.settings
                )
            self.charge_points[self.settings.cpid] = charge_point
            await charge_point.start()
        else:
            _LOGGER.info(f"Charger {cp_id} reconnected to {self.host}:{self.port}.")
            charge_point = self.charge_points[self.settings.cpid]
            await charge_point.reconnect(websocket)
        _LOGGER.info(f"Charger {cp_id} disconnected from {self.host}:{self.port}.")

    def get_metric(self, cp_id: str, measurand: str):
        """Return last known value for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].value
        return None

    def del_metric(self, cp_id: str, measurand: str):
        """Set given measurand to None."""
        if cp_id in self.charge_points:
            self.charge_points[cp_id]._metrics[measurand].value = None
        return None

    def get_unit(self, cp_id: str, measurand: str):
        """Return unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].unit
        return None

    def get_ha_unit(self, cp_id: str, measurand: str):
        """Return home assistant unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].ha_unit
        return None

    def get_extra_attr(self, cp_id: str, measurand: str):
        """Return last known extra attributes for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].extra_attr
        return None

    def get_available(self, cp_id: str):
        """Return whether the charger is available."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].status == STATE_OK
        return False

    def get_supported_features(self, cp_id: str):
        """Return what profiles the charger supports."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].supported_features
        return 0

    async def set_max_charge_rate_amps(self, cp_id: str, value):
        """Set the maximum charge rate in amps."""
        try:
            value = float(value)
        except (ValueError, TypeError):
            _LOGGER.error(f"Invalid value provided for charge rate: {value}")
            return False

        if value <= 0:
            _LOGGER.error(f"Charge rate must be greater than 0: {value}")
            return False

        if cp_id in self.charge_points:
            _LOGGER.info(f"Setting charge rate to {value} amps for charger {cp_id}.")
            return await self.charge_points[cp_id].set_charge_rate(limit_amps=value)

        _LOGGER.error(f"Charge point {cp_id} not found.")
        return False

    async def set_charger_state(self, cp_id: str, service_name: str, state: bool = True):
        """Carry out requested service/state change on connected charger."""
        valid_services = {
            csvcs.service_availability.name,
            csvcs.service_charge_start.name,
            csvcs.service_charge_stop.name,
            csvcs.service_reset.name,
            csvcs.service_unlock.name,
        }
        if service_name not in valid_services:
            _LOGGER.error(f"Invalid service name provided: {service_name}")
            return False

        if cp_id not in self.charge_points:
            _LOGGER.error(f"Charge point {cp_id} not found for service {service_name}.")
            return False

        charge_point = self.charge_points[cp_id]
        if service_name == csvcs.service_availability.name:
            return await charge_point.set_availability(state)
        if service_name == csvcs.service_charge_start.name:
            return await charge_point.start_transaction()
        if service_name == csvcs.service_charge_stop.name:
            return await charge_point.stop_transaction()
        if service_name == csvcs.service_reset.name:
            return await charge_point.reset()
        if service_name == csvcs.service_unlock.name:
            return await charge_point.unlock()

        return False

    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.id)},
        }

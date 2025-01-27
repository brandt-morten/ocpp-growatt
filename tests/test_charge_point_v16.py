"""Implement a test by a simulating an OCPP 1.6 chargepoint."""

import asyncio
from datetime import datetime, UTC  # timedelta,

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import websockets

from custom_components.ocpp.button import BUTTONS
from custom_components.ocpp.const import DOMAIN as OCPP_DOMAIN
from custom_components.ocpp.enums import ConfigurationKey, HAChargerServices as csvcs
from custom_components.ocpp.number import NUMBERS
from custom_components.ocpp.switch import SWITCHES
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cpclass, call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfileStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    DiagnosticsStatus,
    FirmwareStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    TriggerMessageStatus,
    UnlockStatus,
)

from .const import MOCK_CONFIG_DATA, MOCK_CONFIG_DATA_2
from .charge_point_test import set_switch, press_button, set_number
import contextlib


@pytest.mark.timeout(90)  # Set timeout for this test
async def test_cms_responses_v16(hass, socket_enabled):
    """Test central system responses to a charger."""

    async def test_switches(hass, cs, socket_enabled):
        """Test switch operations."""
        for switch in SWITCHES:
            await set_switch(hass, cs, switch.key, True)
            await asyncio.sleep(1)
            await set_switch(hass, cs, switch.key, False)

    async def test_buttons(hass, cs, socket_enabled):
        """Test button operations."""
        for button in BUTTONS:
            await press_button(hass, cs, button.key)

    async def test_services(hass, cs, socket_enabled):
        """Test service operations."""
        SERVICES = [
            csvcs.service_update_firmware,
            csvcs.service_configure,
            csvcs.service_get_configuration,
            csvcs.service_get_diagnostics,
            csvcs.service_clear_profile,
            csvcs.service_data_transfer,
            csvcs.service_set_charge_rate,
        ]
        for service in SERVICES:
            data = {}
            if service == csvcs.service_update_firmware:
                data = {"firmware_url": "http://www.charger.com/firmware.bin"}
            if service == csvcs.service_configure:
                data = {"ocpp_key": "WebSocketPingInterval", "value": "60"}
            if service == csvcs.service_get_configuration:
                data = {"ocpp_key": "UnknownKeyTest"}
            if service == csvcs.service_get_diagnostics:
                data = {"upload_url": "https://webhook.site/abc"}
            if service == csvcs.service_data_transfer:
                data = {"vendor_id": "ABC"}
            if service == csvcs.service_set_charge_rate:
                data = {"limit_amps": 30}

            await hass.services.async_call(
                OCPP_DOMAIN,
                service.value,
                service_data=data,
                blocking=True,
            )
        # test additional set charge rate options
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_set_charge_rate,
            service_data={"limit_watts": 3000},
            blocking=True,
        )
        # test custom charge profile for advanced use
        prof = {
            "chargingProfileId": 8,
            "stackLevel": 6,
            "chargingProfileKind": "Relative",
            "chargingProfilePurpose": "ChargePointMaxProfile",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16.0}],
            },
        }
        data = {"custom_profile": str(prof)}
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_set_charge_rate,
            service_data=data,
            blocking=True,
        )

        for number in NUMBERS:
            # test setting value of number slider
            await set_number(hass, cs, number.key, 10)

    # Test MOCK_CONFIG_DATA_2
    # Create a mock entry so we don't have to go through config flow
    config_entry2 = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=MOCK_CONFIG_DATA_2,
        entry_id="test_cms2",
        title="test_cms2",
    )
    config_entry2.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry2.entry_id)
    await hass.async_block_till_done()

    # no subprotocol central system assumes ocpp1.6 charge point
    # NB each new config entry will trigger async_update_entry
    # if the charger measurands differ from the config entry
    # which causes the websocket server to close/restart with a
    # ConnectionClosedOK exception, hence it needs to be passed/suppressed
    async with websockets.connect(
        "ws://127.0.0.1:9002/CP_1_nosub",
    ) as ws2:
        # use a different id for debugging
        assert ws2.subprotocol is None
        cp2 = ChargePoint("CP_1_no_subprotocol", ws2)
        with contextlib.suppress(
            asyncio.TimeoutError, websockets.exceptions.ConnectionClosedOK
        ):
            await asyncio.wait_for(
                asyncio.gather(
                    cp2.start(),
                    cp2.send_boot_notification(),
                    cp2.send_authorize(),
                    cp2.send_heartbeat(),
                    cp2.send_status_notification(),
                    cp2.send_firmware_status(),
                    cp2.send_data_transfer(),
                    cp2.send_start_transaction(),
                    cp2.send_stop_transaction(),
                    cp2.send_meter_periodic_data(),
                ),
                timeout=5,
            )
        await ws2.close()
    await asyncio.sleep(1)
    if entry := hass.config_entries.async_get_entry(config_entry2.entry_id):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN, data=MOCK_CONFIG_DATA, entry_id="test_cms", title="test_cms"
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    cs = hass.data[OCPP_DOMAIN][config_entry.entry_id]

    # unsupported subprotocol raises websockets exception
    with pytest.raises(websockets.exceptions.InvalidStatus):
        await websockets.connect(
            "ws://127.0.0.1:9000/CP_1_unsup",
            subprotocols=["ocpp0.0"],
        )

    # test restore feature of meter_start and active_tranasction_id.
    async with websockets.connect(
        "ws://127.0.0.1:9000/CP_1_res_vals",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint("CP_1_restore_values", ws)
        cp.active_transactionId = None
        # send None values
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_meter_periodic_data(),
                ),
                timeout=3,
            )
        # check if None
        assert cs.get_metric("test_cpid", "Energy.Meter.Start") is None
        assert cs.get_metric("test_cpid", "Transaction.Id") is None
        # send new data
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.send_start_transaction(12344),
                    cp.send_meter_periodic_data(),
                ),
                timeout=3,
            )
        # save for reference the values for meter_start and transaction_id
        saved_meter_start = int(cs.get_metric("test_cpid", "Energy.Meter.Start"))
        saved_transactionId = int(cs.get_metric("test_cpid", "Transaction.Id"))
        # delete current values from api memory
        cs.del_metric("test_cpid", "Energy.Meter.Start")
        cs.del_metric("test_cpid", "Transaction.Id")
        # send new data
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.send_meter_periodic_data(),
                ),
                timeout=3,
            )
        await ws.close()

    # check if restored old values from HA when api have lost the values, i.e. simulated reboot of HA
    assert int(cs.get_metric("test_cpid", "Energy.Meter.Start")) == saved_meter_start
    assert int(cs.get_metric("test_cpid", "Transaction.Id")) == saved_transactionId

    await asyncio.sleep(1)

    # test ocpp messages sent from charger to cms
    async with websockets.connect(
        "ws://127.0.0.1:9000/CP_1_norm",
        subprotocols=["ocpp1.5", "ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint("CP_1_normal", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_boot_notification(),
                    cp.send_authorize(),
                    cp.send_heartbeat(),
                    cp.send_status_notification(),
                    cp.send_security_event(),
                    cp.send_firmware_status(),
                    cp.send_data_transfer(),
                    cp.send_start_transaction(12345),
                    cp.send_meter_err_phases(),
                    cp.send_meter_line_voltage(),
                    cp.send_meter_periodic_data(),
                    # add delay to allow meter data to be processed
                    cp.send_stop_transaction(1),
                ),
                timeout=5,
            )
        await ws.close()
    assert int(cs.get_metric("test_cpid", "Energy.Active.Import.Register")) == int(
        1305570 / 1000
    )
    assert int(cs.get_metric("test_cpid", "Energy.Session")) == int(
        (54321 - 12345) / 1000
    )
    assert int(cs.get_metric("test_cpid", "Current.Import")) == 0
    assert int(cs.get_metric("test_cpid", "Voltage")) == 228
    assert cs.get_unit("test_cpid", "Energy.Active.Import.Register") == "kWh"
    assert cs.get_ha_unit("test_cpid", "Power.Reactive.Import") == "var"
    assert cs.get_unit("test_cpid", "Power.Reactive.Import") == "var"
    assert cs.get_metric("unknown_cpid", "Energy.Active.Import.Register") is None
    assert cs.get_unit("unknown_cpid", "Energy.Active.Import.Register") is None
    assert cs.get_extra_attr("unknown_cpid", "Energy.Active.Import.Register") is None
    assert int(cs.get_supported_features("unknown_cpid")) == 0
    assert (
        await asyncio.wait_for(
            cs.set_max_charge_rate_amps("unknown_cpid", 0), timeout=1
        )
        is False
    )
    assert (
        await asyncio.wait_for(
            cs.set_charger_state("unknown_cpid", csvcs.service_clear_profile, False),
            timeout=1,
        )
        is False
    )

    await asyncio.sleep(1)
    # test ocpp messages sent from cms to charger, through HA switches/services
    # should reconnect as already started above
    # test processing of clock aligned meter data
    async with websockets.connect(
        "ws://127.0.0.1:9000/CP_1_serv",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = ChargePoint("CP_1_services", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cs.charge_points[cs.settings.cpid].trigger_boot_notification(),
                    cs.charge_points[cs.settings.cpid].trigger_status_notification(),
                    test_switches(hass, cs, socket_enabled),
                    test_services(hass, cs, socket_enabled),
                    test_buttons(hass, cs, socket_enabled),
                    cp.send_meter_clock_data(),
                ),
                timeout=5,
            )
        await ws.close()
    assert int(cs.get_metric("test_cpid", "Frequency")) == 50
    assert (
        float(cs.get_metric("test_cpid", "Energy.Active.Import.Register")) == 1101.452
    )

    await asyncio.sleep(1)

    # test ocpp messages sent from charger that don't support errata 3.9
    # i.e. "Energy.Meter.Start" starts from 0 for each session and "Energy.Active.Import.Register"
    # reports starting from 0 Wh for every new transaction id. Total main meter values are without transaction id.
    async with websockets.connect(
        "ws://127.0.0.1:9000/CP_1_non_er_3.9",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint("CP_1_non_errata_3.9", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_start_transaction(0),
                    cp.send_meter_periodic_data(),
                    cp.send_main_meter_clock_data(),
                    # add delay to allow meter data to be processed
                    cp.send_stop_transaction(1),
                ),
                timeout=5,
            )
        await ws.close()

    # Last sent "Energy.Active.Import.Register" value without transaction id should be here.
    assert int(cs.get_metric("test_cpid", "Energy.Active.Import.Register")) == int(
        67230012 / 1000
    )
    assert cs.get_unit("test_cpid", "Energy.Active.Import.Register") == "kWh"

    # Last sent "Energy.Active.Import.Register" value with transaction id should be here.
    assert int(cs.get_metric("test_cpid", "Energy.Session")) == int(1305570 / 1000)
    assert cs.get_unit("test_cpid", "Energy.Session") == "kWh"

    await asyncio.sleep(1)

    # test ocpp messages sent from charger that don't support errata 3.9 with meter values with kWh as energy unit
    async with websockets.connect(
        "ws://127.0.0.1:9000/CP_1_non_er_3.9",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint("CP_1_non_errata_3.9", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_start_transaction(0),
                    cp.send_meter_energy_kwh(),
                    cp.send_meter_clock_data(),
                    # add delay to allow meter data to be processed
                    cp.send_stop_transaction(1),
                ),
                timeout=5,
            )
        await ws.close()

    assert int(cs.get_metric("test_cpid", "Energy.Active.Import.Register")) == 1101
    assert int(cs.get_metric("test_cpid", "Energy.Session")) == 11
    assert cs.get_unit("test_cpid", "Energy.Active.Import.Register") == "kWh"

    # test ocpp rejection messages sent from charger to cms
    cs.charge_points["test_cpid"].received_boot_notification = False
    cs.charge_points["test_cpid"].post_connect_success = False
    async with websockets.connect(
        "ws://127.0.0.1:9000/CP_1_error",
        subprotocols=["ocpp1.6"],
    ) as ws:
        with contextlib.suppress(
            asyncio.TimeoutError, websockets.exceptions.ConnectionClosedOK
        ):
            cp = ChargePoint("CP_1_error", ws)
            cp.accept = False
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cs.charge_points[cs.settings.cpid].trigger_boot_notification(),
                    cs.charge_points[cs.settings.cpid].trigger_status_notification(),
                    test_switches(hass, cs, socket_enabled),
                    test_services(hass, cs, socket_enabled),
                    test_buttons(hass, cs, socket_enabled),
                ),
                timeout=3,
            )
        await ws.close()

    await asyncio.sleep(1)

    # test services when charger is unavailable
    await asyncio.sleep(1)
    await test_services(hass, cs, socket_enabled)
    if entry := hass.config_entries.async_get_entry(config_entry.entry_id):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()


class ChargePoint(cpclass):
    """Representation of real client Charge Point."""

    def __init__(self, id, connection, response_timeout=30):
        """Init extra variables for testing."""
        super().__init__(id, connection)
        self.active_transactionId: int = 0
        self.accept: bool = True

    @on(Action.get_configuration)
    def on_get_configuration(self, key, **kwargs):
        """Handle a get configuration requests."""
        if key[0] == ConfigurationKey.supported_feature_profiles.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {
                            "key": key[0],
                            "readonly": False,
                            "value": "Core,FirmwareManagement,LocalAuthListManagement,Reservation,SmartCharging,RemoteTrigger,Dummy",
                        }
                    ]
                )
            else:
                # use to test TypeError handling
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.heartbeat_interval.value:
            return call_result.GetConfiguration(
                configuration_key=[{"key": key[0], "readonly": False, "value": "300"}]
            )
        if key[0] == ConfigurationKey.number_of_connectors.value:
            return call_result.GetConfiguration(
                configuration_key=[{"key": key[0], "readonly": False, "value": "1"}]
            )
        if key[0] == ConfigurationKey.web_socket_ping_interval.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "60"}
                    ]
                )
            else:
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.meter_values_sampled_data.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {
                            "key": key[0],
                            "readonly": False,
                            "value": "Energy.Active.Import.Register",
                        }
                    ]
                )
            else:
                raise Exception
        if key[0] == ConfigurationKey.meter_value_sample_interval.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "60"}
                    ]
                )
            else:
                return call_result.GetConfiguration(
                    configuration_key=[{"key": key[0], "readonly": True, "value": "60"}]
                )
        if (
            key[0]
            == ConfigurationKey.charging_schedule_allowed_charging_rate_unit.value
        ):
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "Current"}
                    ]
                )
            else:
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.authorize_remote_tx_requests.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "false"}
                    ]
                )
            else:
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.charge_profile_max_stack_level.value:
            return call_result.GetConfiguration(
                configuration_key=[{"key": key[0], "readonly": False, "value": "3"}]
            )
        return call_result.GetConfiguration(
            configuration_key=[{"key": key[0], "readonly": False, "value": ""}]
        )

    @on(Action.change_configuration)
    def on_change_configuration(self, key, **kwargs):
        """Handle a get configuration request."""
        if self.accept is True:
            if key == ConfigurationKey.meter_values_sampled_data.value:
                return call_result.ChangeConfiguration(
                    ConfigurationStatus.reboot_required
                )
            else:
                return call_result.ChangeConfiguration(ConfigurationStatus.accepted)
        else:
            return call_result.ChangeConfiguration(ConfigurationStatus.rejected)

    @on(Action.change_availability)
    def on_change_availability(self, **kwargs):
        """Handle change availability request."""
        if self.accept is True:
            return call_result.ChangeAvailability(AvailabilityStatus.accepted)
        else:
            return call_result.ChangeAvailability(AvailabilityStatus.rejected)

    @on(Action.unlock_connector)
    def on_unlock_connector(self, **kwargs):
        """Handle unlock request."""
        if self.accept is True:
            return call_result.UnlockConnector(UnlockStatus.unlocked)
        else:
            return call_result.UnlockConnector(UnlockStatus.unlock_failed)

    @on(Action.reset)
    def on_reset(self, **kwargs):
        """Handle change availability request."""
        if self.accept is True:
            return call_result.Reset(ResetStatus.accepted)
        else:
            return call_result.Reset(ResetStatus.rejected)

    @on(Action.remote_start_transaction)
    def on_remote_start_transaction(self, **kwargs):
        """Handle remote start request."""
        if self.accept is True:
            self.task = asyncio.create_task(self.send_start_transaction())
            return call_result.RemoteStartTransaction(RemoteStartStopStatus.accepted)
        else:
            return call_result.RemoteStopTransaction(RemoteStartStopStatus.rejected)

    @on(Action.remote_stop_transaction)
    def on_remote_stop_transaction(self, **kwargs):
        """Handle remote stop request."""
        if self.accept is True:
            return call_result.RemoteStopTransaction(RemoteStartStopStatus.accepted)
        else:
            return call_result.RemoteStopTransaction(RemoteStartStopStatus.rejected)

    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, **kwargs):
        """Handle set charging profile request."""
        if self.accept is True:
            return call_result.SetChargingProfile(ChargingProfileStatus.accepted)
        else:
            return call_result.SetChargingProfile(ChargingProfileStatus.rejected)

    @on(Action.clear_charging_profile)
    def on_clear_charging_profile(self, **kwargs):
        """Handle clear charging profile request."""
        if self.accept is True:
            return call_result.ClearChargingProfile(ClearChargingProfileStatus.accepted)
        else:
            return call_result.ClearChargingProfile(ClearChargingProfileStatus.unknown)

    @on(Action.trigger_message)
    def on_trigger_message(self, **kwargs):
        """Handle trigger message request."""
        if self.accept is True:
            return call_result.TriggerMessage(TriggerMessageStatus.accepted)
        else:
            return call_result.TriggerMessage(TriggerMessageStatus.rejected)

    @on(Action.update_firmware)
    def on_update_firmware(self, **kwargs):
        """Handle update firmware request."""
        return call_result.UpdateFirmware()

    @on(Action.get_diagnostics)
    def on_get_diagnostics(self, **kwargs):
        """Handle get diagnostics request."""
        return call_result.GetDiagnostics()

    @on(Action.data_transfer)
    def on_data_transfer(self, **kwargs):
        """Handle get data transfer request."""
        if self.accept is True:
            return call_result.DataTransfer(DataTransferStatus.accepted)
        else:
            return call_result.DataTransfer(DataTransferStatus.rejected)

    async def send_boot_notification(self):
        """Send a boot notification."""
        request = call.BootNotification(
            charge_point_model="Optimus", charge_point_vendor="The Mobility House"
        )
        resp = await self.call(request)
        assert resp.status == RegistrationStatus.accepted

    async def send_heartbeat(self):
        """Send a heartbeat."""
        request = call.Heartbeat()
        resp = await self.call(request)
        assert len(resp.current_time) > 0

    async def send_authorize(self):
        """Send an authorize request."""
        request = call.Authorize(id_tag="test_cp")
        resp = await self.call(request)
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted

    async def send_firmware_status(self):
        """Send a firmware status notification."""
        request = call.FirmwareStatusNotification(status=FirmwareStatus.downloaded)
        resp = await self.call(request)
        assert resp is not None

    async def send_diagnostics_status(self):
        """Send a diagnostics status notification."""
        request = call.DiagnosticsStatusNotification(status=DiagnosticsStatus.uploaded)
        resp = await self.call(request)
        assert resp is not None

    async def send_data_transfer(self):
        """Send a data transfer."""
        request = call.DataTransfer(
            vendor_id="The Mobility House",
            message_id="Test123",
            data="Test data transfer",
        )
        resp = await self.call(request)
        assert resp.status == DataTransferStatus.accepted

    async def send_start_transaction(self, meter_start: int = 12345):
        """Send a start transaction notification."""
        request = call.StartTransaction(
            connector_id=1,
            id_tag="test_cp",
            meter_start=meter_start,
            timestamp=datetime.now(tz=UTC).isoformat(),
        )
        resp = await self.call(request)
        self.active_transactionId = resp.transaction_id
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted.value

    async def send_status_notification(self):
        """Send a status notification."""
        request = call.StatusNotification(
            connector_id=0,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.suspended_ev,
            timestamp=datetime.now(tz=UTC).isoformat(),
            info="Test info",
            vendor_id="The Mobility House",
            vendor_error_code="Test error",
        )
        resp = await self.call(request)
        request = call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.charging,
            timestamp=datetime.now(tz=UTC).isoformat(),
            info="Test info",
            vendor_id="The Mobility House",
            vendor_error_code="Test error",
        )
        resp = await self.call(request)
        request = call.StatusNotification(
            connector_id=2,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.available,
            timestamp=datetime.now(tz=UTC).isoformat(),
            info="Test info",
            vendor_id="The Mobility House",
            vendor_error_code="Available",
        )
        resp = await self.call(request)

        assert resp is not None

    async def send_meter_periodic_data(self):
        """Send periodic meter data notification."""
        n = 0
        while self.active_transactionId == 0 and n < 2:
            await asyncio.sleep(1)
            n += 1
        request = call.MeterValues(
            connector_id=1,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "1305590.000",
                            "context": "Sample.Periodic",
                            "measurand": "Energy.Active.Import.Register",
                            "location": "Outlet",
                            "unit": "Wh",
                        },
                        {
                            "value": "20.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L1",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L2",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L3",
                        },
                        {
                            "value": "16.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Offered",
                            "location": "Outlet",
                            "unit": "A",
                        },
                        {
                            "value": "50.010",
                            "context": "Sample.Periodic",
                            "measurand": "Frequency",
                            "location": "Outlet",
                        },
                        {
                            "value": "",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "kW",
                        },
                        {
                            "value": "",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "W",
                            "phase": "L1",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "W",
                            "phase": "L2",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "W",
                            "phase": "L3",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Factor",
                            "location": "Outlet",
                        },
                        {
                            "value": "38.500",
                            "context": "Sample.Periodic",
                            "measurand": "Temperature",
                            "location": "Body",
                            "unit": "Celsius",
                        },
                        {
                            "value": "228.000",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L1-N",
                        },
                        {
                            "value": "228.000",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L2-N",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L3-N",
                        },
                        {
                            "value": "89.00",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Reactive.Import",
                            "unit": "var",
                        },
                        {
                            "value": "0.010",
                            "context": "Transaction.Begin",
                            "unit": "kWh",
                        },
                        {
                            "value": "1305570.000",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_line_voltage(self):
        """Send line voltages."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=1,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "395.900",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L1-L2",
                        },
                        {
                            "value": "396.300",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L2-L3",
                        },
                        {
                            "value": "398.900",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L3-L1",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_err_phases(self):
        """Send erroneous voltage phase."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=1,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "230",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L1",
                        },
                        {
                            "value": "23",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L1-N",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_energy_kwh(self):
        """Send periodic energy meter value with kWh unit."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=1,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "unit": "kWh",
                            "value": "11",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_main_meter_clock_data(self):
        """Send periodic main meter value. Main meter values dont have transaction_id."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=1,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "67230012",
                            "context": "Sample.Clock",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "location": "Inlet",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_clock_data(self):
        """Send periodic meter data notification."""
        self.active_transactionId = 0
        request = call.MeterValues(
            connector_id=1,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "measurand": "Voltage",
                            "context": "Sample.Clock",
                            "unit": "V",
                            "value": "228.490",
                        },
                        {
                            "measurand": "Power.Active.Import",
                            "context": "Sample.Clock",
                            "unit": "W",
                            "value": "0.000",
                        },
                        {
                            "measurand": "Energy.Active.Import.Register",
                            "context": "Sample.Clock",
                            "unit": "kWh",
                            "value": "1101.452",
                        },
                        {
                            "measurand": "Current.Import",
                            "context": "Sample.Clock",
                            "unit": "A",
                            "value": "0.054",
                        },
                        {
                            "measurand": "Frequency",
                            "context": "Sample.Clock",
                            "value": "50.000",
                        },
                    ],
                },
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_stop_transaction(self, delay: int = 0):
        """Send a stop transaction notification."""
        # add delay to allow meter data to be processed
        await asyncio.sleep(delay)
        n = 0
        while self.active_transactionId == 0 and n < 2:
            await asyncio.sleep(1)
            n += 1
        request = call.StopTransaction(
            meter_stop=54321,
            timestamp=datetime.now(tz=UTC).isoformat(),
            transaction_id=self.active_transactionId,
            reason="EVDisconnected",
            id_tag="test_cp",
        )
        resp = await self.call(request)
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted.value

    async def send_security_event(self):
        """Send a security event notification."""
        request = call.SecurityEventNotification(
            type="SettingSystemTime",
            timestamp="2022-09-29T20:58:29Z",
            tech_info="BootNotification",
        )
        await self.call(request)

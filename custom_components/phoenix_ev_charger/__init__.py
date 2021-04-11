"""The PEVC Modbus Integration."""
import asyncio
import logging
import threading
from datetime import timedelta
from typing import Optional

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (CONF_HOST, CONF_NAME, CONF_PORT,
                                 CONF_SCAN_INTERVAL)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from pymodbus.client.sync import ModbusTcpClient
from pymodbus.constants import Endian
from pymodbus.exceptions import ConnectionException
from pymodbus.payload import BinaryPayloadDecoder

from .const import (DEFAULT_NAME, DEFAULT_SCAN_INTERVAL, DEVICE_STATUSSES,
                    DOMAIN, CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)

_LOGGER = logging.getLogger(__name__)

PEVC_MODBUS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT): cv.string,
        vol.Optional(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): cv.positive_int,
        vol.Required(CONF_DEVICE_MODEL, default=DEFAULT_DEVICE_MODEL): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({cv.slug: PEVC_MODBUS_SCHEMA})}, extra=vol.ALLOW_EXTRA
)

PLATFORMS = ["sensor"]


async def async_setup(hass, config):
    """Set up the PEVC modbus component."""
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a PEVC mobus."""
    host = entry.data[CONF_HOST]
    name = entry.data[CONF_NAME]
    port = entry.data[CONF_PORT]
    scan_interval = entry.data[CONF_SCAN_INTERVAL]
    model = entry.data[CONF_DEVICE_MODEL]

    _LOGGER.debug("Setup %s.%s", DOMAIN, name)

    hub = PEVCModbusHub(
        hass, name, host, port, scan_interval
    )
    """Register the hub."""
    hass.data[DOMAIN][name] = {"hub": hub}

    for component in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )
    return True


async def async_unload_entry(hass, entry):
    """Unload PEVC mobus entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(
                    entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if not unload_ok:
        return False

    hass.data[DOMAIN].pop(entry.data["name"])
    return True


class PEVCModbusHub:
    """Thread safe wrapper class for pymodbus."""

    def __init__(
        self,
        hass,
        name,
        host,
        port,
        scan_interval,
    ):
        """Initialize the Modbus hub."""
        self._hass = hass
        self._client = ModbusTcpClient(host=host, port=port, timeout=5)
        self._lock = threading.Lock()
        self._name = name
        self._scan_interval = timedelta(seconds=scan_interval)
        self._unsub_interval_method = None
        self._sensors = []
        self.data = {}

    @callback
    def async_add_pevc_sensor(self, update_callback):
        """Listen for data updates."""
        # This is the first sensor, set up interval.
        if not self._sensors:
            self.connect()
            self._unsub_interval_method = async_track_time_interval(
                self._hass, self.async_refresh_modbus_data, self._scan_interval
            )

        self._sensors.append(update_callback)

    @callback
    def async_remove_pevc_sensor(self, update_callback):
        """Remove data update."""
        self._sensors.remove(update_callback)

        if not self._sensors:
            """stop the interval timer upon removal of last sensor"""
            self._unsub_interval_method()
            self._unsub_interval_method = None
            self.close()

    async def async_refresh_modbus_data(self, _now: Optional[int] = None) -> None:
        """Time to update."""
        if not self._sensors:
            return

        update_result = self.read_modbus_data()

        if update_result:
            for update_callback in self._sensors:
                update_callback()

    @property
    def name(self):
        """Return the name of this hub."""
        return self._name

    def close(self):
        """Disconnect client."""
        with self._lock:
            self._client.close()

    def connect(self):
        """Connect client."""
        with self._lock:
            self._client.connect()

    def read_holding_registers(self, unit, address, count):
        """Read holding registers."""
        with self._lock:
            kwargs = {"unit": unit} if unit else {}
            return self._client.read_holding_registers(address, count, **kwargs)

    def read_input_registers(self, unit, address, count):
        """Read holding registers."""
        with self._lock:
            kwargs = {"unit": unit} if unit else {}
            return self._client.read_input_registers(address, count, **kwargs)

    def calculate_value(self, value, sf):
        return value * 10 ** sf

    def swapAscii(self, str, length):
        ostr = ''
        for i in range(int(length/2)):
            ostr = ostr + str[i*2+1] 
            ostr = ostr + str[i*2] 
        return ostr;

    def read_modbus_data(self):
        return (
            self.read_modbus_inverter_data()
            and self.read_modbus_realtime_data()
        )

    def read_modbus_inverter_data(self):
        connected = False
        try:
            inverter_data = self.read_holding_registers(
                unit=255, address=300, count=32)
            connected = True
        except ConnectionException as ex:
            _LOGGER.error('Reading inverter data failed! Inverter is unreachable.')
            connected = False

        if connected:
            if not inverter_data.isError():
                decoder = BinaryPayloadDecoder.fromRegisters(
                    inverter_data.registers, byteorder=Endian.Big
                )

                chargingCurrent = decoder.decode_16bit_uint()
                self.data["current"] = chargingCurrent
                macaddress1 = decoder.decode_16bit_uint()
                macaddress2 = decoder.decode_16bit_uint()
                macaddress3 = decoder.decode_16bit_uint()

                sn = decoder.decode_string(12).decode('ascii')
                self.data["sn"] = str( self.swapAscii( sn, 12))

                devName = decoder.decode_string(10).decode('ascii')
                self.data["devicename"] = str( self.swapAscii( devName, 10))

                ip = decoder.decode_32bit_uint()
                ip = decoder.decode_32bit_uint()
                subnet = decoder.decode_32bit_uint()
                subnet = decoder.decode_32bit_uint()
                gateway = decoder.decode_32bit_uint()
                gateway = decoder.decode_32bit_uint()

                digOut = decoder.decode_16bit_uint() 
                digOut = decoder.decode_16bit_uint() 
                digOut = decoder.decode_16bit_uint() 
                digOut = decoder.decode_16bit_uint() 

                return True
            else:

                return False
        else:
            mpvmode = 0
            self.data["devstate"] = mpvmode

            if mpvmode in DEVICE_STATUSSES:
                self.data["devstate"] = DEVICE_STATUSSES[mpvmode]
            else:
                self.data["devstate"] = "Unknown"

            power = 0
            self.data["power"] = power

            return True

    def read_modbus_realtime_data(self):
        connected = False
        try:
            realtime_data = self.read_input_registers(
                unit=255, address=100, count=57)
            connected = True
        except ConnectionException as ex:
            _LOGGER.error('Reading realtime data failed! Inverter is unreachable.')
            connected = False

        if False and connected:
            if not realtime_data.isError():
                decoder = BinaryPayloadDecoder.fromRegisters(
                    realtime_data.registers, byteorder=Endian.Big
                )

                mpvmode = decoder.decode_string(2).decode('ascii')
                self.data["mpvmode"] = mpvmode

                if mpvmode in DEVICE_STATUSSES:
                    self.data["mpvstatus"] = DEVICE_STATUSSES[mpvmode]
                else:
                    self.data["mpvstatus"] = "Unknown"

                maxCableCurrent = decoder.decode_16bit_uint()
                chargingTime    = decoder.decode_32bit_uint()
                dipSwitches     = decoder.decode_16bit_uint()

                fwVersion = decoder.decode_string(4).decode('ascii')
                self.data["fwvers"] = str( self.swapAscii( fwVersion, 4))
                
                errcodes1 = decoder.decode_16bit_uint()
                return True
            else:

                return False
        else:
            mpvmode = 0
            self.data["devstate"] = mpvmode

            if mpvmode in DEVICE_STATUSSES:
                self.data["devstate"] = DEVICE_STATUSSES[mpvmode]
            else:
                self.data["devstate"] = "Unknown"

            power = 0
            self.data["power"] = power

            return True


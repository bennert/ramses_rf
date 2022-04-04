#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Heating devices.
"""

import logging
from symtable import Class

from .const import __dev_mode__
from .devices_base import Device
from .devices_heat import HEAT_CLASS_BY_KLASS, class_dev_heat
from .devices_hvac import HVAC_CLASS_BY_KLASS, class_dev_hvac
from .protocol import Address, Message
from .schema import SZ_KLASS

DEV_MODE = __dev_mode__  # and False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


_CLASS_BY_KLASS = HEAT_CLASS_BY_KLASS | HVAC_CLASS_BY_KLASS


def zx_device_factory(gwy, dev_addr: Address, msg: Message = None, **schema) -> Class:
    """Return the device class for a given device id/msg/schema."""

    def class_dev(
        dev_addr: Address,
        msg: Message = None,
        eavesdrop: bool = False,
        **schema,
    ) -> Class:
        """Return the best device class for a given device id/msg/schema."""

        # a specified device class always takes precidence (even if it is wrong)...
        if klass := _CLASS_BY_KLASS[schema.get(SZ_KLASS)]:
            _LOGGER.debug(f"Using configured dev class for: {dev_addr} ({klass})")
            return klass

        try:  # or, is it a well-known CH/DHW class, derived from the device type...
            if klass := class_dev_heat(dev_addr.type, msg=msg, eavesdrop=eavesdrop):
                _LOGGER.debug(f"Using default dev class for: {dev_addr} ({klass})")
                return klass  # includes HeatDevice
        except TypeError:
            pass

        try:  # or, a HVAC class, eavesdropped from the message code/payload...
            if klass := class_dev_hvac(dev_addr.type, msg=msg, eavesdrop=eavesdrop):
                _LOGGER.warning(
                    f"Using eavesdropped dev class for: {dev_addr} ({klass})"
                )
                return klass  # includes HvacDevice
        except TypeError:
            pass

        # otherwise, use the default device class...
        _LOGGER.warning(f"Using generic dev class for: {dev_addr} ({Device})")
        return Device

    return class_dev(
        dev_addr,
        msg=msg,
        eavesdrop=gwy.config.enable_eavesdropping,
        **schema,
    )

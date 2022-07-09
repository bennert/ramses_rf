#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

The serial to RF gateway (HGI80, not RFG100).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from asyncio.futures import Future
from concurrent import futures
from datetime import datetime as dt
from threading import Lock
from typing import Callable, Optional

from ramses_rf.devices_base import DeviceHeat, DeviceHvac

from .const import DONT_CREATE_MESSAGES, SZ_DEVICE_ID, SZ_DEVICES, __dev_mode__
from .devices import Device, zx_device_factory
from .helpers import schedule_task, shrink
from .message import Message, process_msg
from .protocol import (
    SZ_POLLER_TASK,
    Address,
    Command,
    create_msg_stack,
    create_pkt_stack,
    is_valid_dev_id,
    set_logger_timesource,
    set_pkt_logging_config,
)
from .protocol.address import HGI_DEV_ADDR, NON_DEV_ADDR, NUL_DEV_ADDR
from .schema import (
    DEBUG_MODE,
    ENFORCE_KNOWN_LIST,
    INPUT_FILE,
    SCH_TRAITS,
    SZ_ALIAS,
    SZ_BLOCK_LIST,
    SZ_CLASS,
    SZ_CONFIG,
    SZ_FAKED,
    SZ_KNOWN_LIST,
    SZ_MAIN_CONTROLLER,
    SZ_ORPHANS,
    load_config,
    load_schema,
)
from .systems import System

# skipcq: PY-W2000
from .protocol import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    F9,
    FA,
    FC,
    FF,
    _313F,
)


DEV_MODE = __dev_mode__ and False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


class Engine:
    """The engine class."""

    def __init__(self, serial_port, loop=None) -> None:

        self._loop = loop or asyncio.get_running_loop()

        self.ser_name = serial_port
        self._input_file = None

        self.pkt_protocol, self.pkt_transport = None, None
        self.msg_protocol, self.msg_transport = None, None

        self._engine_lock = Lock()
        self._engine_state = None

    def __str__(self) -> str:
        return (self.hgi or HGI_DEV_ADDR).id + f" ({self.ser_name})"

    def _setup_event_handlers(self) -> None:  # HACK: for dev/test only
        def handle_exception(loop, context):
            """Handle exceptions on any platform."""
            _LOGGER.error("handle_exception(): Caught: %s", context["message"])

            exc = context.get("exception")
            if exc:
                raise exc

        if DEV_MODE:
            _LOGGER.debug("_setup_event_handlers(): Creating exception handler...")
            self._loop.set_exception_handler(handle_exception)

    def _dt_now(self):
        # return dt.now()
        return self.pkt_protocol._dt_now() if self.pkt_protocol else dt.now()

    def create_client(self, msg_handler) -> tuple[Callable, Callable]:
        """Create a client protocol for the RAMSES-II message transport."""
        return create_msg_stack(self, msg_handler)

    async def start(self) -> None:
        self._start()

    def _start(self) -> None:
        """Initiate ad-hoc sending, and (polled) receiving."""

        (_LOGGER.warning if DEV_MODE else _LOGGER.debug)("ENGINE: Starting poller...")

        if self.ser_name:  # source of packets is a serial port
            pkt_receiver = (
                self.msg_transport.get_extra_info(self.msg_transport.READER)
                if self.msg_transport
                else None
            )
            self.pkt_protocol, self.pkt_transport = create_pkt_stack(
                self, pkt_receiver, ser_port=self.ser_name
            )  # TODO: can raise SerialException
            if self.msg_transport:
                self.msg_transport._set_dispatcher(self.pkt_protocol.send_data)

        else:  # if self._input_file:
            pkt_receiver = (
                self.msg_transport.get_extra_info(self.msg_transport.READER)
                if self.msg_transport
                else None
            )
            self.pkt_protocol, self.pkt_transport = create_pkt_stack(
                self, pkt_receiver, packet_log=self._input_file
            )
            set_logger_timesource(self.pkt_protocol._dt_now)
            _LOGGER.warning("Datetimes maintained as most recent packet log timestamp")

    async def stop(self) -> None:
        self._stop()

        if (task := self.pkt_source) and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _stop(self) -> None:
        """Cancel all outstanding tasks."""

        if self.msg_transport:
            self.msg_transport.close()  # ? .abort()

        if self.pkt_transport:
            self.pkt_transport.close()  # ? .abort()

    def _pause(self, *args) -> None:
        """Pause the (unpaused) engine or raise a RuntimeError."""

        (_LOGGER.info if DEV_MODE else _LOGGER.debug)("ENGINE: Pausing engine...")

        if not self._engine_lock.acquire(blocking=False):
            raise RuntimeError("Unable to pause engine, failed to acquire lock")

        if self._engine_state is not None:
            self._engine_lock.release()
            raise RuntimeError("Unable to pause engine, it is already paused")

        self._engine_state, callback = (None, None), None
        self._engine_lock.release()

        if self.pkt_protocol:
            self.pkt_protocol.pause_writing()
            self.pkt_protocol._callback, callback = None, self.pkt_protocol._callback

        self._engine_state = (callback, args)

    def _resume(self) -> tuple:  # FIXME: not atomic
        """Resume the (paused) engine or raise a RuntimeError."""

        (_LOGGER.info if DEV_MODE else _LOGGER.debug)("ENGINE: Resuming engine...")

        # if not self.ser_name:
        #     raise RuntimeError("Unable to resume engine, no serial port configured")

        if not self._engine_lock.acquire(timeout=0.1):
            raise RuntimeError("Unable to resume engine, failed to acquire lock")

        if self._engine_state is None:
            self._engine_lock.release()
            raise RuntimeError("Unable to resume engine, it was not paused")

        callback, args = self._engine_state
        self._engine_lock.release()

        if self.pkt_protocol:
            self.pkt_protocol._callback = callback  # self.msg_transport._pkt_receiver
            self.pkt_protocol.resume_writing()

        self._engine_state = None

        return args

    @property
    def hgi(self) -> Optional[Device]:
        """Return the HGI80-compatible gateway device."""

        if self.pkt_protocol and self.pkt_protocol._hgi80[SZ_DEVICE_ID]:
            return self.device_by_id.get(self.pkt_protocol._hgi80[SZ_DEVICE_ID])

    @property
    def pkt_source(self) -> asyncio.Task:
        if t := self.msg_transport:
            return t.get_extra_info(t.WRITER)

    @staticmethod
    def create_cmd(verb, device_id, code, payload, **kwargs) -> Command:
        """Make a command addressed to device_id."""

        try:
            return Command.from_attrs(verb, device_id, code, payload, **kwargs)
        except (
            AssertionError,
            AttributeError,
            LookupError,
            TypeError,
            ValueError,
        ) as exc:
            _LOGGER.exception(f"create_cmd(): {exc}")

    def send_cmd(self, cmd: Command, callback: Callable = None, **kwargs) -> Future:
        """Send a command with the option to return any response message via callback.

        Response packets, if any (an RP/I will follow an RQ/W), and have the same code.
        This routine is thread safe.
        """

        if not self.msg_protocol:
            raise RuntimeError("there is no message protocol")

        return asyncio.run_coroutine_threadsafe(
            self.msg_protocol.send_data(cmd, callback=callback, **kwargs),
            self._loop,
        )

    async def async_send_cmd(self, cmd: Command, **kwargs) -> Message:
        """Send a command with the option to not wait for a response message.

        Response packets, if any, follow an RQ/W (as an RP/I), and have the same code.
        This routine is thread safe.
        """

        # def callback(fut):
        #     print(fut.result())

        fut = self.send_cmd(cmd, _make_awaitable=True, **kwargs)
        # fut.add_done_callback(callback)

        while True:
            try:
                result = fut.result(timeout=0)

            # except futures.CancelledError:  # fut ?was cancelled by a higher layer
            #     break

            except futures.TimeoutError:  # fut/cmd has not yet completed
                pass  # should be a pass

            except TimeoutError as exc:  # raised by send_cmd()
                raise TimeoutError(f"cmd ({cmd.tx_header}) timed out: {exc}")

            except Exception as exc:
                _LOGGER.error(f"cmd ({cmd.tx_header}) raised an exception: {exc!r}")
                raise exc

            else:
                _LOGGER.debug(f"cmd ({cmd.tx_header}) returned: {result!r})")
                return result

            await asyncio.sleep(0.001)


class Gateway(Engine):
    """The gateway class."""

    def __init__(self, serial_port, loop=None, **kwargs) -> None:

        if kwargs.pop(DEBUG_MODE, None):
            _LOGGER.setLevel(logging.DEBUG)  # should be INFO?
        _LOGGER.debug("Starting RAMSES RF, **kwargs = %s", kwargs)

        super().__init__(serial_port, loop=loop)

        self._tasks: list = []

        self._input_file = kwargs.pop(INPUT_FILE, None)

        self._include: dict = {}  # the provided known_list (?and used as an allow_list)
        self._exclude: dict = {}  # the provided block_list

        (self.config, self._schema, self._include, self._exclude) = load_config(
            self.ser_name, self._input_file, **kwargs
        )
        self._unwanted = [NON_DEV_ADDR.id, NUL_DEV_ADDR.id, "01:000001"]

        set_pkt_logging_config(
            cc_console=self.config.reduce_processing >= DONT_CREATE_MESSAGES,
            **self.config.packet_log,
        )

        if self.config.reduce_processing < DONT_CREATE_MESSAGES:
            self.msg_protocol, self.msg_transport = self.create_client(process_msg)

        # if self.config.reduce_processing > 0:
        self._tcs: System = None
        self.devices: list[Device] = []
        self.device_by_id: dict = {}

        self._setup_event_handlers()

        load_schema(self, **self._schema)

        self._db = None
        self._cur = None

    def __repr__(self) -> str:
        return super().__str__()

    def __str__(self) -> str:
        return (self.hgi or HGI_DEV_ADDR).id

    def _setup_event_handlers(self) -> None:  # HACK: for dev/test only
        async def handle_sig_posix(sig):
            """Handle signals on posix platform."""
            _LOGGER.debug("Received a signal (%s), processing...", sig.name)

            if sig == signal.SIGUSR1:
                _LOGGER.info("Schema: \r\n%s", {self.tcs.id: self.tcs.schema})
                _LOGGER.info("Params: \r\n%s", {self.tcs.id: self.tcs.params})
                _LOGGER.info("Status: \r\n%s", {self.tcs.id: self.tcs.status})

            elif sig == signal.SIGUSR2:
                _LOGGER.info("Status: \r\n%s", {self.tcs.id: self.tcs.status})

        super()._setup_event_handlers()

        _LOGGER.debug("_setup_event_handlers(): Creating signal handlers...")
        if os.name == "posix":  # full support
            for sig in [signal.SIGUSR1, signal.SIGUSR2]:
                self._loop.add_signal_handler(
                    sig, lambda sig=sig: self._loop.create_task(handle_sig_posix(sig))
                )
        elif os.name == "nt":  # supported, but YMMV
            _LOGGER.warning("Be aware, YMMV with Windows...")
        else:  # unsupported
            raise RuntimeError(f"Unsupported OS for this module: {os.name}")

    async def start(self, *, start_discovery=True) -> None:
        def initiate_discovery(dev_list, sys_list) -> None:
            _LOGGER.debug("ENGINE: Initiating/enabling discovery...")

            # [d._start_discovery_poller() for d in devs]
            for device in dev_list:
                device._start_discovery_poller()

            for system in sys_list:
                system._start_discovery_poller()
                for zone in system.zones:
                    zone._start_discovery_poller()
                if system.dhw:
                    system.dhw._start_discovery_poller()

        await super().start()

        if not self.ser_name:  # wait until have processed the entire packet log...
            await self.pkt_transport.get_extra_info(SZ_POLLER_TASK)

        elif start_discovery:  # source of packets is a serial port
            initiate_discovery(self.devices, self.systems)

    async def stop(self) -> None:
        """Cancel all outstanding tasks."""
        # if self._engine_state is None:
        #     self._pause()

        if tasks := [t.cancel() for t in self._tasks if not t.done()]:
            try:  # TODO: except asyncio.CancelledError:
                await asyncio.gather(*tasks)
            except TypeError:  # HACK
                pass
        await super().stop()

    def _pause(self, *args, clear_state=False) -> None:
        """Pause the (unpaused) gateway."""

        super()._pause(
            self.config.disable_discovery, self.config.disable_sending, *args
        )
        self.config.disable_discovery = True
        self.config.disable_sending = True

        if clear_state:
            self._clear_state()

    def _resume(self) -> tuple:
        """Resume the (paused) gateway."""

        (
            self.config.disable_discovery,
            self.config.disable_sending,
            *args,
        ) = super()._resume()

        return args

    def _clear_state(self) -> None:
        _LOGGER.warning("ENGINE: Clearing exisiting schema/state...")

        self._tcs = None
        self.devices = []
        self.device_by_id = {}

    def get_state(self, include_expired=None) -> tuple[dict, dict]:

        (_LOGGER.warning if DEV_MODE else _LOGGER.debug)("ENGINE: Getting state...")
        self._pause()

        result = self._get_state(include_expired=include_expired)

        self._resume()
        (_LOGGER.warning if DEV_MODE else _LOGGER.debug)("ENGINE: Got schema/state.")

        return result

    def _get_state(self, include_expired=None) -> tuple[dict, dict]:

        msgs = [m for device in self.devices for m in device._msg_db]

        for system in self.systems:
            msgs.extend(list(system._msgs.values()))
            msgs.extend([m for z in system.zones for m in z._msgs.values()])
            # msgs.extend([m for z in system.dhw for m in z._msgs.values()])

        # BUG: assumes pkts have unique dtms: may not be true for contrived logs...
        pkts = {
            f"{repr(msg._pkt)[:26]}": f"{repr(msg._pkt)[27:]}"
            for msg in msgs
            if msg.verb in (I_, RP)
            and (
                include_expired or msg.code == _313F or not msg._expired
            )  # 313F will be expired, but will be useful for back-back restarts
        }  # BUG: assumes pkts have unique dtms

        return self.schema, dict(sorted(pkts.items()))

    async def set_state(self, packets, *, schema=None) -> None:
        (_LOGGER.warning if DEV_MODE else _LOGGER.info)("ENGINE: Setting state...")

        if schema is None:  # TODO: also for known_list (device traits)?
            schema = shrink(self.schema)

        self._pause(clear_state=True)

        load_schema(self, **schema)
        self._set_state(packets, schema=schema)

        self._resume()
        (_LOGGER.warning if DEV_MODE else _LOGGER.info)("ENGINE: Set state.")

    async def _set_state(self, packets: dict, *, schema=None) -> None:

        pkt_receiver = (
            self.msg_transport.get_extra_info(self.msg_transport.READER)
            if self.msg_transport
            else None
        )
        _, tmp_transport = create_pkt_stack(self, pkt_receiver, packet_dict=packets)
        await tmp_transport.get_extra_info(SZ_POLLER_TASK)

        # self.msg_transport._clear_write_buffer()  # TODO: shouldn't be needed
        self.msg_protocol._prev_msg = None  # TODO: move to pause/resume?

    def get_device(
        self, dev_id, *, msg=None, parent=None, child_id=None, is_sensor=None
    ) -> Device:  # TODO: **schema) -> Device:
        """Return a device, create it if required.

        First, use the traits to create/update it, then pass it any msg to handle.
        All devices have traits, but only controllers (CTL, UFC) have a schema.

        Devices are uniquely identified by a device id.
        If a device is created, attach it to the gateway.
        """

        def check_filter_lists(dev_id: str) -> None:
            """Raise an LookupError if a device_id is filtered out by a list."""

            if dev_id in self._unwanted:
                # _LOGGER.warning()
                raise LookupError(f"Can't create {dev_id}: it unwanted or invalid")

            if self.config.enforce_known_list and (
                dev_id not in self._include
                and dev_id != self.pkt_protocol._hgi80[SZ_DEVICE_ID]
            ):
                self._unwanted.append(dev_id)
                raise LookupError(
                    f"Can't create {dev_id}: it is not an allowed device_id"
                    f" (if required, add it to the {SZ_KNOWN_LIST})"
                )

            if dev_id in self._exclude:
                self._unwanted.append(dev_id)
                raise LookupError(
                    f"Can't create {dev_id}: it is a blocked device_id"
                    f" (if required, remove it from the {SZ_BLOCK_LIST})"
                )

        check_filter_lists(dev_id)
        traits = SCH_TRAITS(self._include.get(dev_id, {}))

        dev = self.device_by_id.get(dev_id)
        if not dev:
            dev = zx_device_factory(self, Address(dev_id), msg=msg, **traits)

        # TODO: the exact order of the follwoing may need refining...

        # if schema:  # Step 2: Only controllers have a schema...
        #     dev._update_schema(**schema)  # TODO: schema/traits

        if parent or child_id:
            dev.set_parent(parent, child_id=child_id, is_sensor=is_sensor)

        if traits.get(SZ_FAKED):
            dev._make_fake()

        if msg:
            dev._handle_msg(msg)

        return dev

    @property
    def tcs(self) -> Optional[System]:
        """Return the primary TCS, if any."""

        if self._tcs is None and self.systems:
            self._tcs = self.systems[0]
        return self._tcs

    @property
    def known_list(self) -> dict:
        """Return the working known_list (a superset of the provided known_list).

        Unlike orphans, which are always instantiated when a schema is loaded, these
        devices may/may not exist. However, if they are ever instantiated, they should
        be given these traits.
        """

        result = self._include
        result.update(
            {
                d.id: {k: d.traits[k] for k in (SZ_CLASS, SZ_ALIAS, SZ_FAKED)}
                for d in self.devices
                if not self.config.enforce_known_list or d.id in self._include
            }
        )
        return result

    @property
    def system_by_id(self) -> dict:
        return {d.id: d.tcs for d in self.devices if getattr(d, "tcs", None)}

    @property
    def systems(self) -> list:
        return [d.tcs for d in self.devices if getattr(d, "tcs", None)]

    @property
    def _config(self) -> dict:
        """Return the working configuration.

        Includes:
         - config
         - schema (everything else)
         - known_list
         - block_list
        """

        return {
            "_gateway_id": self.hgi.id if self.hgi else None,
            SZ_MAIN_CONTROLLER: self.tcs.id if self.tcs else None,
            SZ_CONFIG: {ENFORCE_KNOWN_LIST: self.config.enforce_known_list},
            SZ_KNOWN_LIST: self.known_list,
            SZ_BLOCK_LIST: [{k: v} for k, v in self._exclude.items()],
            "_unwanted": sorted(self.pkt_protocol._unwanted),
            "_unwanted_alt": sorted(self._unwanted),
        }

    @property
    def schema(self) -> dict:
        """Return the global schema.

        Orphans are devices that 'exist' but don't yet have a place in the schema
        hierachy (if ever): therefore, they are instantiated when the schema is loaded,
        just like the other devices in the schema.
        """

        schema = {SZ_MAIN_CONTROLLER: self.tcs.ctl.id if self.tcs else None}

        for tcs in self.systems:
            schema[tcs.ctl.id] = tcs.schema

        schema[f"{SZ_ORPHANS}_heat"] = sorted(
            [
                d.id
                for d in self.devices
                if not getattr(d, "tcs", None)
                and isinstance(d, DeviceHeat)
                and d._is_present
            ]
        )

        schema[f"{SZ_ORPHANS}_hvac"] = sorted(
            [d.id for d in self.devices if isinstance(d, DeviceHvac) and d._is_present]
        )

        return schema

    @property
    def params(self) -> dict:
        return {SZ_DEVICES: {d.id: d.params for d in sorted(self.devices)}}

    @property
    def status(self) -> dict:
        return {SZ_DEVICES: {d.id: d.status for d in sorted(self.devices)}}

    def send_cmd(self, cmd: Command, callback: Callable = None, **kwargs) -> Future:
        """Send a command with the option to return any response via callback."""

        if self.config.disable_sending:
            raise RuntimeError("sending is disabled")

        fut = super().send_cmd(cmd, callback, **kwargs)

        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(fut)
        return fut

    def fake_device(
        self, device_id, create_device=False, start_binding=False
    ) -> Device:
        """Create a faked device, and optionally set it to binding mode.

        Will make any neccesary changed to the device lists.
        """
        # TODO: what about using the HGI

        if not is_valid_dev_id(device_id):
            raise TypeError(f"The device id is not valid: {device_id}")

        if create_device and device_id in self.device_by_id:
            raise LookupError(f"The device id already exists: {device_id}")
        elif not create_device and device_id not in self.device_by_id:
            raise LookupError(f"The device id does not exist: {device_id}")

        if self.config.enforce_known_list and device_id not in self._include:
            self._include[device_id] = {}
        elif device_id in self._exclude:
            del self._exclude[device_id]

        return self.get_device(device_id)._make_fake(bind=start_binding)

    def add_task(self, fnc, *args, delay=None, period=None, **kwargs) -> None:
        """Start a task after delay seconds and then repeat it every period seconds."""
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(
            schedule_task(fnc, *args, delay=delay, period=period, **kwargs)
        )


class GatewayWithSql(Gateway):
    def __init__(self, *args, **kwargs) -> None:

        import sqlite3

        self._db = sqlite3.connect("file::memory:?cache=shared")
        self._cur = self._db.cursor()

    async def start(self, *, start_discovery=True) -> None:
        super().start(start_discovery=start_discovery)

    async def stop(self) -> None:
        super().stop()

    def _get_state(self, include_expired=None) -> tuple[dict, dict]:
        pass

    async def _set_state(self, packets, *, schema=None) -> None:
        pass

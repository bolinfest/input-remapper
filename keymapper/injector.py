#!/usr/bin/python3
# -*- coding: utf-8 -*-
# key-mapper - GUI for device specific keyboard mappings
# Copyright (C) 2020 sezanzeb <proxima@hip70890b.de>
#
# This file is part of key-mapper.
#
# key-mapper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# key-mapper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with key-mapper.  If not, see <https://www.gnu.org/licenses/>.


"""Keeps injecting keycodes in the background based on the mapping."""


import re
import asyncio
import time
import subprocess
# By using processes instead of threads, the mappings are
# automatically copied, so that they can be worked with in the ui
# without breaking the device. And it's possible to terminate processes.
import multiprocessing

import evdev

from keymapper.logger import logger
from keymapper.getdevices import get_devices
from keymapper.state import custom_mapping, system_mapping


DEV_NAME = 'key-mapper'
DEVICE_CREATED = 1
FAILED = 2
DEVICE_SKIPPED = 3

# offset between xkb and linux keycodes. linux keycodes are lower
KEYCODE_OFFSET = 8


def _grab(path):
    """Try to grab, repeat a few times with time inbetween on failure."""
    attempts = 0
    while True:
        device = evdev.InputDevice(path)
        try:
            # grab to avoid e.g. the disabled keycode of 10 to confuse
            # X, especially when one of the buttons of your mouse also
            # uses 10. This also avoids having to load an empty xkb
            # symbols file to prevent writing any unwanted keys.
            device.grab()
            break
        except IOError:
            attempts += 1
            logger.debug('Failed attemt to grab %s %d', path, attempts)

        if attempts >= 4:
            logger.error('Cannot grab %s, it is possibly in use', path)
            return None

        # it might take a little time until the device is free if
        # it was previously grabbed.
        time.sleep(0.15)
    return device


def _modify_capabilities(device):
    """Adds all keycode into a copy of a devices capabilities."""
    # copy the capabilities because the keymapper_device is going
    # to act like the device.
    capabilities = device.capabilities(absinfo=False)
    # However, make sure that it supports all keycodes, not just some
    # random ones, because the mapping could contain anything.
    # That's why I avoid from_device for this
    capabilities[evdev.ecodes.EV_KEY] = list(evdev.ecodes.keys.keys())

    # just like what python-evdev does in from_device
    if evdev.ecodes.EV_SYN in capabilities:
        del capabilities[evdev.ecodes.EV_SYN]
    if evdev.ecodes.EV_FF in capabilities:
        del capabilities[evdev.ecodes.EV_FF]

    return capabilities


def _is_device_mapped(device, mapping):
    """Check if this device has capabilities that are being mapped.

    Parameters
    ----------
    device : evdev.InputDevice
    mapping : Mapping
    """
    capabilities = device.capabilities(absinfo=False)[evdev.ecodes.EV_KEY]
    needed = False
    for keycode, _ in mapping:
        if keycode - KEYCODE_OFFSET in capabilities:
            needed = True
    if not needed:
        logger.debug('No need to grab %s', device.path)
    return needed


def _start_injecting_worker(path, pipe, mapping):
    """Inject keycodes for one of the virtual devices.

    Parameters
    ----------
    path : string
        path in /dev to read keycodes from
    pipe : multiprocessing.Pipe
        pipe to send status codes over
    """
    # evdev needs asyncio to work
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        device = _grab(path)
    except Exception as error:
        logger.error(error)
        pipe.send(FAILED)
        return

    if device is None:
        pipe.send(FAILED)
        return

    if not _is_device_mapped(device, mapping):
        # skipping reading and checking on events from those devices
        # may be beneficial for performance.
        pipe.send(DEVICE_SKIPPED)
        return

    keymapper_device = evdev.UInput(
        name=f'key-mapper {device.name}',
        phys='key-mapper',
        events=_modify_capabilities(device)
    )

    pipe.send(DEVICE_CREATED)

    logger.debug(
        'Started injecting into %s, fd %s',
        keymapper_device.device.path, keymapper_device.fd
    )

    for event in device.read_loop():
        if event.type != evdev.ecodes.EV_KEY:
            keymapper_device.write(event.type, event.code, event.value)
            # this already includes SYN events, so need to syn here again
            continue

        if event.value == 2:
            # linux does them itself, no need to trigger them
            continue

        input_keycode = event.code + KEYCODE_OFFSET

        character = mapping.get_character(input_keycode)

        if character is None:
            # unknown keycode, forward it
            target_keycode = input_keycode
        else:
            target_keycode = system_mapping.get_keycode(character)
            if target_keycode is None:
                logger.error(
                    'Cannot find character %s in the internal mapping',
                    character
                )
                continue

        logger.spam(
            'got code:%s value:%s, maps to code:%s char:%s',
            event.code + KEYCODE_OFFSET, event.value, target_keycode, character
        )

        keymapper_device.write(
            evdev.ecodes.EV_KEY,
            target_keycode - KEYCODE_OFFSET,
            event.value
        )
        keymapper_device.syn()


def is_numlock_on():
    """Get the current state of the numlock."""
    xset_q = subprocess.check_output(['xset', 'q']).decode()
    num_lock_status = re.search(
        r'Num Lock:\s+(.+?)\s',
        xset_q
    )

    if num_lock_status is not None:
        return num_lock_status[1] == 'on'

    return False


def toggle_numlock():
    """Turn the numlock on or off."""
    try:
        subprocess.check_output(['numlockx', 'toggle'])
    except FileNotFoundError:
        # doesn't seem to be installed everywhere
        logger.debug('numlockx not found, trying to inject a keycode')
        # and this doesn't always work.
        device = evdev.UInput(
            name=f'key-mapper numlock-control',
            phys='key-mapper',
        )
        device.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_NUMLOCK, 1)
        device.syn()
        device.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_NUMLOCK, 0)
        device.syn()


def ensure_numlock(func):
    """Decorator to reset the numlock to its initial state afterwards."""
    def wrapped(*args, **kwargs):
        # for some reason, grabbing a device can modify the num lock state.
        # remember it and apply back later
        numlock_before = is_numlock_on()
        result = func(*args, **kwargs)
        numlock_after = is_numlock_on()
        if numlock_after != numlock_before:
            logger.debug('Reverting numlock status to %s', numlock_before)
            toggle_numlock()
        return result
    return wrapped


class KeycodeInjector:
    """Keeps injecting keycodes in the background based on the mapping."""
    @ensure_numlock
    def __init__(self, device, mapping=custom_mapping):
        """Start injecting keycodes based on custom_mapping."""
        self.device = device
        self.virtual_devices = []
        self.processes = []

        paths = get_devices()[self.device]['paths']

        logger.info('Starting injecting the mapping for %s', self.device)

        # Watch over each one of the potentially multiple devices per hardware
        for path in paths:
            pipe = multiprocessing.Pipe()
            worker = multiprocessing.Process(
                target=_start_injecting_worker,
                args=(path, pipe[1], mapping)
            )
            worker.start()
            # wait for the process to notify creation of the new injection
            # device, to keep the logs in order.
            status = pipe[0].recv()
            if status == DEVICE_CREATED:
                self.processes.append(worker)
            else:
                worker.join()

        if len(self.processes) == 0:
            raise OSError('Could not grab any device')

    @ensure_numlock
    def stop_injecting(self):
        """Stop injecting keycodes."""
        logger.info('Stopping injecting keycodes for device %s', self.device)
        for i, process in enumerate(self.processes):
            if process is None:
                continue

            if process.is_alive():
                process.terminate()
                self.processes[i] = None

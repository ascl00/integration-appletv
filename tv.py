import asyncio
import base64
import logging
import random

from enum import IntEnum

from pyee import AsyncIOEventEmitter

import pyatv
import pyatv.const

from pyatv.interface import PushListener
from pyatv.interface import DeviceListener
from pyatv.interface import AudioListener
from pyatv.interface import KeyboardListener

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

BACKOFF_MAX = 30
BACKOFF_SEC = 2
ARTWORK_WIDTH = 400
ARTWORK_HEIGHT = 400

class EVENTS(IntEnum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    PAIRED = 3
    ERROR = 4
    UPDATE = 5
    VOLUME_CHANGED = 6

class AppleTv(object):
    def __init__(self, loop):
        self._loop = loop
        self.events = AsyncIOEventEmitter(self._loop)
        self._isOn = False
        self._atv = None
        self.name = ""
        self.identifier = None
        self._credentials = []
        self._connectTask = None
        self._connectionAttempts = 0
        self.pairingAtv = None
        self._pairingProcess = None
        self._polling = None
        self._pollInterval = 2
        self._prevUpdateHash = None
        self._appList = {}

    def backoff(self):
        if self._connectionAttempts * BACKOFF_SEC == BACKOFF_MAX:
            self._connectionAttempts = 0

        return self._connectionAttempts * BACKOFF_SEC

    def playstatus_update(self, updater, playstatus):
        LOG.debug("Push update")
        LOG.debug(str(playstatus))
        _ = asyncio.ensure_future(self._processUpdate(playstatus))

        
    def playstatus_error(self, updater, exception):
        LOG.debug(str(exception))

    def connection_lost(self, exception):
        LOG.exception("Lost connection")
        self.events.emit(EVENTS.DISCONNECTED, self.identifier)
        _ = asyncio.ensure_future(self._stopPolling())
        if self._atv:
            self._atv.close()
            self._atv = None
        self._startConnectLoop()

    def connection_closed(self):
        LOG.debug("Connection closed!")

    def volume_update(self, old_level, new_level):
        LOG.debug('Volume level: %d', new_level)
        # TODO: implement me

    def outputdevices_update(self, old_devices, new_devices):
        # print('Output devices changed from {0:s} to {1:s}'.format(old_devices, new_devices))
        pass
        # TODO: implement me

    def focusstate_update(self, old_state, new_state):
        # print('Focus state changed from {0:s} to {1:s}'.format(old_state, new_state))
        pass
        # TODO: implement me

    async def findAtv(self, identifier):
        atvs = await pyatv.scan(self._loop, identifier=identifier)
        if not atvs:
            return None
        else:
            return atvs[0]

    async def init(self, identifier, credentials = [], name = ""):
        self.identifier = identifier
        self._credentials = credentials
        self.name = name

    def addCredentials(self, credentials):
        self._credentials.append(credentials)

    def getCredentials(self):
        return self._credentials
    
    async def startPairing(self, protocol, name):
        LOG.debug('Pairing started')
        self._pairingProcess = await pyatv.pair(self.pairingAtv, protocol, self._loop, name=name)
        await self._pairingProcess.begin()

        if self._pairingProcess.device_provides_pin:
            LOG.debug('Device provides PIN')
            return 0
        else:
            LOG.debug('We provide PIN')
            pin = random.randint(1000,9999)
            self._pairingProcess.pin(pin)
            return pin

    async def enterPin(self, pin):
        LOG.debug('Entering PIN')
        self._pairingProcess.pin(pin)

    async def finishPairing(self):
        LOG.debug('Pairing finished')
        res = None

        await self._pairingProcess.finish()

        if self._pairingProcess.has_paired:
            LOG.debug('Paired with device!')
            res = self._pairingProcess.service
        else:
            LOG.warning('Did not pair with device')
            self.events.emit(EVENTS.ERROR, self.identifier, 'Could not pair with device')

        await self._pairingProcess.close()
        self._pairingProcess = None
        return res

    async def connect(self):
        if self._isOn is True:
            return 
        self._isOn = True
        self.events.emit(EVENTS.CONNECTING, self.identifier)
        self._startConnectLoop()

    def _startConnectLoop(self):
        if not self._connectTask and self._atv is None and self._isOn:
            self._connectTask = asyncio.create_task(self._connectLoop())
        else:
            LOG.debug('Not starting connect loop (Atv: %s, isOn: %s)', self._atv is None, self._isOn)

    async def _connectLoop(self):
        LOG.debug('Starting connect loop')
        while self._isOn and self._atv is None:
            await self._connectOnce()
            if self._atv is not None:
                break
            self._connectionAttempts += 1
            backoff = self.backoff()
            LOG.debug('Trying to connect again in %ds', backoff)
            await asyncio.sleep(backoff)
        
        LOG.debug('Connect loop ended')
        self._connectTask = None

        await self._getUpdate()

        self._atv.push_updater.listener = self
        self._atv.push_updater.start()
        self._atv.listener = self
        self._atv.audio.listener = self
        self._atv.keyboard.listener = self

        self._connectionAttempts = 0

        await self._startPolling()

        self.events.emit(EVENTS.CONNECTED, self.identifier)
        LOG.debug("Connected")

    async def _connectOnce(self):
        try:
            if conf := await self.findAtv(self.identifier):
                await self._connect(conf)
        except pyatv.exceptions.AuthenticationError:
            LOG.warning('Could not connect: auth error')
            await self.disconnect()
            return
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.warning('Could not connect')
            self._atv = None

    async def _connect(self, conf):
        missingProtocols = []

        for credential in self._credentials:
            protocol = None
            if credential['protocol'] == 'companion':
                protocol = pyatv.const.Protocol.Companion
            elif credential['protocol'] == 'airplay':
                protocol = pyatv.const.Protocol.AirPlay

            if conf.get_service(protocol) is not None:
                LOG.debug('Setting credentials for protocol: %s', protocol)
                conf.set_credentials(protocol, credential['credentials'])
            else:
                missingProtocols.append(protocol.name)

        if missingProtocols:
            missingProtocolsStr = ", ".join(missingProtocols)
            LOG.warning('Protocols %s not yet found for %s, trying later', missingProtocolsStr, conf.name)

        LOG.debug("Connecting to device %s", conf.name)
        self._atv = await pyatv.connect(conf, self._loop)


    async def disconnect(self):
        LOG.debug('Disconnecting from device')
        self._isOn = False
        await self._stopPolling()

        try:
            if self._atv:
                self._atv.close()
                self._atv = None
            if self._connectTask:
                self._connectTask.cancel()
                self._connectTask = None
            self.events.emit(EVENTS.DISCONNECTED, self.identifier)
        except Exception:
            LOG.exception('An error occured while disconnecting')


    async def _startPolling(self):
        if self._atv is None:
            LOG.warning('Polling not started, AppleTv object is None')
            self.events.emit(EVENTS.ERROR, 'Polling not started, AppleTv object is None')
            return
        
        await asyncio.sleep(2)
        self._polling = self._loop.create_task(self._pollWorker())
        LOG.debug('Polling started')


    async def _stopPolling(self):
        if self._polling:
            self._polling.cancel()
            self._polling = None
            LOG.debug('Polling stopped')
        else:
            LOG.debug('Polling was already stopped')


    async def _getUpdate(self):
        LOG.debug('Manually getting update')
        update = {}

        try:
            data = await self._atv.metadata.playing()
        except:
            LOG.warning('Could not get metadata yet')
            return

        try:
            artwork = await self._atv.metadata.artwork(width=ARTWORK_WIDTH, height=ARTWORK_HEIGHT)
            artwork_encoded = 'data:image/png;base64,' + base64.b64encode(artwork.bytes).decode('utf-8')
            update['artwork'] = artwork_encoded
        except:
            LOG.warning('Error while updating the artwork')

        update['total_time'] = data.total_time
        update['title'] = data.title

        if data.artist is not None:
            update['artist'] = data.artist
        else:
            update['artist'] = ""
        
        if data.album is not None:
            update['album'] = data.album
        else:
            update['album'] = ""

        if data.media_type is not None:
            update['media_type'] = data.media_type

        if data:
            self.events.emit(EVENTS.UPDATE, update)


    async def _processUpdate(self, data):
        LOG.debug('Push update')

        update = {}

        if self._atv.power.power_state is pyatv.const.PowerState.On:
            update['state'] = data.device_state
            if update['state'] == pyatv.const.DeviceState.Playing:
                self._pollInterval = 2
            else:
                self._pollInterval = 10

        update['position'] = data.position

        # image operations are expensive, so we only do it when the hash changed
        if data.hash != self._prevUpdateHash:
            try:
                artwork = await self._atv.metadata.artwork(width=ARTWORK_WIDTH, height=ARTWORK_HEIGHT)
                artwork_encoded = 'data:image/png;base64,' + base64.b64encode(artwork.bytes).decode('utf-8')
                update['artwork'] = artwork_encoded
            except:
                LOG.warning('Error while updating the artwork')

        update['total_time'] = data.total_time
        update['title'] = data.title

        if data.artist is not None:
            update['artist'] = data.artist
        else:
            update['artist'] = ""
        
        if data.album is not None:
            update['album'] = data.album
        else:
            update['album'] = ""

        if data.media_type is not None:
            update['media_type'] = data.media_type

        # TODO: data.genre
        # TODO: data.repeat: All, Off, Track
        # TODO: data.shuffle

        self._prevUpdateHash = data.hash
        self.events.emit(EVENTS.UPDATE, update)


    async def _pollWorker(self): 
        while True and self._atv is not None:
            update = {}
            
            if self._atv.power.power_state is pyatv.const.PowerState.Off:
                update['state'] = self._atv.power.power_state
            else:
                try:
                    data = await self._atv.metadata.playing()
                    update['state'] = data.device_state                
                    update['sourceList'] = []
                except:
                    LOG.debug('Error while getting playing metadata')

                if self._atv.features.in_state(pyatv.const.FeatureState.Available, pyatv.const.FeatureName.AppList):
                    try:
                        appList = await self._atv.apps.app_list()
                        for app in appList:
                            self._appList[app.name] = app.identifier
                            update['sourceList'].append(app.name)
                    except pyatv.exceptions.NotSupportedError:
                        LOG.warning('App list is not supported')
                    except pyatv.exceptions.ProtocolError:
                        LOG.warning('App list: protocol error')

                if self._isFeatureAvailable(pyatv.const.FeatureName.App):
                    update['source'] = self._atv.metadata.app.name


            self.events.emit(EVENTS.UPDATE, update)
            await asyncio.sleep(self._pollInterval)


    def _isFeatureAvailable(self, feature: pyatv.const.FeatureName) -> bool:
        if self._atv:
            return self._atv.features.in_state(pyatv.const.FeatureState.Available, feature)
        return False
                

    async def _commandWrapper(self, fn):     
        if self._atv is None:
            return False
           
        try:
            await fn()
            return True
        except:
            return False
        

    async def turnOn(self):
        return await self._commandWrapper(self._atv.power.turn_on)
    
    async def turnOff(self):
        return await self._commandWrapper(self._atv.power.turn_off)
    
    async def playPause(self):
        return await self._commandWrapper(self._atv.remote_control.play_pause)
    
    async def next(self):
        return await self._commandWrapper(self._atv.remote_control.next)
    
    async def previous(self):
        return await self._commandWrapper(self._atv.remote_control.previous)
    
    async def volumeUp(self):
        return await self._commandWrapper(self._atv.audio.volume_up)
    
    async def volumeDown(self):
        return await self._commandWrapper(self._atv.audio.volume_down)
    
    async def cursorUp(self):
        return await self._commandWrapper(self._atv.remote_control.up)
    
    async def cursorDown(self):
        return await self._commandWrapper(self._atv.remote_control.down)
    
    async def cursorLeft(self):
        return await self._commandWrapper(self._atv.remote_control.left)
    
    async def cursorRight(self):
        return await self._commandWrapper(self._atv.remote_control.right)
    
    async def cursorEnter(self):
        return await self._commandWrapper(self._atv.remote_control.select)
    
    async def home(self):
        return await self._commandWrapper(self._atv.remote_control.home)
    
    async def menu(self):
        return await self._commandWrapper(self._atv.remote_control.menu)
    
    async def channelUp(self):
        return await self._commandWrapper(self._atv.remote_control.channel_up)
    
    async def channelDown(self):
        return await self._commandWrapper(self._atv.remote_control.channel_down)
    
    async def launchApp(self, appName):
        await self._atv.apps.launch_app(self._appList[appName])
        return True
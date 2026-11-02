"""Интерфейсы громкости Windows. Модуль импортируется только в потоке COM: comtypes при импорте настраивает COM в текущем потоке."""
from __future__ import annotations

from ctypes import HRESULT, POINTER, c_float, c_uint, c_void_p
from ctypes.wintypes import BOOL, DWORD
from typing import Any

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown

_CLSID_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_RENDER, _MULTIMEDIA = 0, 1
_CLSCTX_ALL = 23


class IAudioEndpointVolume(IUnknown):
    _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
    _methods_ = [
        COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "notify")),
        COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "notify")),
        COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint), "count")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float, "db"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float, "level"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "db")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "level")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint, "ch"), (["in"], c_float, "db"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint, "ch"), (["in"], c_float, "lv"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint, "ch"), (["out"], POINTER(c_float), "db")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint, "ch"), (["out"], POINTER(c_float), "lv")),
        COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL, "mute"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "mute")),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "ctx"),
                  (["in"], c_void_p, "params"), (["out"], POINTER(POINTER(IUnknown)), "iface")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint, "flow"), (["in"], DWORD, "mask"), (["out"], POINTER(c_void_p), "devices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint, "flow"), (["in"], c_uint, "role"), (["out"], POINTER(POINTER(IMMDevice)), "device")),
    ]


def endpoint() -> Any:
    enumerator = comtypes.CoCreateInstance(_CLSID_ENUMERATOR, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)
    device = enumerator.GetDefaultAudioEndpoint(_RENDER, _MULTIMEDIA)
    raw = device.Activate(IAudioEndpointVolume._iid_, _CLSCTX_ALL, None)
    return raw.QueryInterface(IAudioEndpointVolume)

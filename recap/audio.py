"""recap – WASAPI loopback audio capture.

Captures system audio (loopback) using the Windows Audio Session API and
streams raw PCM data to a pipe (or file) that FFmpeg can consume.

The heavy lifting happens in a background thread so the main thread can
coordinate video capture and the FFmpeg subprocess simultaneously.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import struct
import threading
import time
from io import RawIOBase
from typing import BinaryIO, Optional

from recap.exceptions import AudioCaptureError

log = logging.getLogger(__name__)

# Constants
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
REFTIMES_PER_SEC = 10_000_000
DEVICE_STATE_ACTIVE = 0x00000001

# WAVE format constants
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


class AudioCapture:
    """WASAPI loopback capture of system audio.

    Writes raw PCM (signed 16-bit LE, stereo, 48 kHz) to the provided
    writable binary stream.
    """

    def __init__(self, output_stream: BinaryIO) -> None:
        self._stream = output_stream
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._format_event = threading.Event()
        self._sample_rate: int = 48000
        self._channels: int = 2
        self._bits_per_sample: int = 16

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def bits_per_sample(self) -> int:
        return self._bits_per_sample

    def start(self) -> None:
        """Start capturing audio in a background thread."""
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="recap-audio"
        )
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        """Signal the capture thread to stop."""
        self._stop_event.set()
        self._running = False

    def wait(self, timeout: Optional[float] = None) -> None:
        """Block until the capture thread finishes."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wait_format_ready(self, timeout: float = 10.0) -> bool:
        """Block until the audio device format has been discovered."""
        return self._format_event.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Run WASAPI loopback capture until stopped.

        This method uses comtypes for COM interaction with the Windows
        audio subsystem.
        """
        try:
            self._capture_loop_impl()
        except Exception as exc:
            log.error("Audio capture error: %s", exc, exc_info=True)
            raise AudioCaptureError(str(exc)) from exc

    def _capture_loop_impl(self) -> None:
        import comtypes
        from comtypes import GUID

        CLSID_MMDeviceEnumerator = GUID(
            "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
        )

        # Minimal COM interface definitions for audio capture
        class IMMDevice(comtypes.IUnknown):
            _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = [
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "Activate",
                    (["in"], ctypes.POINTER(comtypes.GUID), "iid"),
                    (["in"], ctypes.wintypes.DWORD, "dwClsCtx"),
                    (["in"], ctypes.c_void_p, "pActivationParams"),
                    (["out"], ctypes.POINTER(ctypes.c_void_p), "ppInterface"),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "OpenPropertyStore",
                    (["in"], ctypes.wintypes.DWORD, "stgmAccess"),
                    (["out"], ctypes.POINTER(ctypes.c_void_p), "ppProperties"),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetId",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.wintypes.LPWSTR),
                        "ppstrId",
                    ),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetState",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.wintypes.DWORD),
                        "pdwState",
                    ),
                ),
            ]

        class IMMDeviceEnumerator(comtypes.IUnknown):
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = [
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "EnumAudioEndpoints",
                    (["in"], ctypes.wintypes.DWORD, "dataFlow"),
                    (["in"], ctypes.wintypes.DWORD, "dwStateMask"),
                    (["out"], ctypes.c_void_p, "ppDevices"),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetDefaultAudioEndpoint",
                    (["in"], ctypes.wintypes.DWORD, "dataFlow"),
                    (["in"], ctypes.wintypes.DWORD, "role"),
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.POINTER(IMMDevice)),
                        "ppEndpoint",
                    ),
                ),
            ]

        # WAVEFORMATEX
        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [
                ("wFormatTag", ctypes.c_ushort),
                ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint),
                ("nAvgBytesPerSec", ctypes.c_uint),
                ("nBlockAlign", ctypes.c_ushort),
                ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort),
            ]

        IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
        IID_IAudioCaptureClient = GUID(
            "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"
        )

        class IAudioClient(comtypes.IUnknown):
            _iid_ = IID_IAudioClient
            _methods_ = [
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "Initialize",
                    (["in"], ctypes.c_uint, "ShareMode"),
                    (["in"], ctypes.c_uint, "StreamFlags"),
                    (["in"], ctypes.c_longlong, "hnsBufferDuration"),
                    (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                    (["in"], ctypes.POINTER(WAVEFORMATEX), "pFormat"),
                    (["in"], ctypes.c_void_p, "AudioSessionGuid"),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetBufferSize",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_uint),
                        "pNumBufferFrames",
                    ),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetStreamLatency",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_longlong),
                        "phnsLatency",
                    ),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetCurrentPadding",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_uint),
                        "pNumPaddingFrames",
                    ),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "IsFormatSupported",
                    (["in"], ctypes.c_uint, "ShareMode"),
                    (["in"], ctypes.POINTER(WAVEFORMATEX), "pFormat"),
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)),
                        "ppClosestMatch",
                    ),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetMixFormat",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)),
                        "ppDeviceFormat",
                    ),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetDevicePeriod",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_longlong),
                        "phnsDefaultDevicePeriod",
                    ),
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_longlong),
                        "phnsMinimumDevicePeriod",
                    ),
                ),
                comtypes.COMMETHOD(
                    [], comtypes.HRESULT, "Start"
                ),
                comtypes.COMMETHOD(
                    [], comtypes.HRESULT, "Stop"
                ),
                comtypes.COMMETHOD(
                    [], comtypes.HRESULT, "Reset"
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "SetEventHandle",
                    (["in"], ctypes.wintypes.HANDLE, "eventHandle"),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetService",
                    (["in"], ctypes.POINTER(comtypes.GUID), "riid"),
                    (["out"], ctypes.POINTER(ctypes.c_void_p), "ppv"),
                ),
            ]

        class IAudioCaptureClient(comtypes.IUnknown):
            _iid_ = IID_IAudioCaptureClient
            _methods_ = [
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetBuffer",
                    (["out"], ctypes.POINTER(ctypes.c_void_p), "ppData"),
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_uint),
                        "pNumFramesToRead",
                    ),
                    (["out"], ctypes.POINTER(ctypes.c_uint), "pdwFlags"),
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_ulonglong),
                        "pu64DevicePosition",
                    ),
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_ulonglong),
                        "pu64QPCPosition",
                    ),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "ReleaseBuffer",
                    (["in"], ctypes.c_uint, "NumFramesRead"),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "GetNextPacketSize",
                    (
                        ["out"],
                        ctypes.POINTER(ctypes.c_uint),
                        "pNumFramesInNextPacket",
                    ),
                ),
            ]

        CLSCTX_ALL = 23
        eRender = 0
        eConsole = 0

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            # Thread already has a COM apartment; that's fine.
            pass
        try:
            enumerator = comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator
            )
            device = enumerator.GetDefaultAudioEndpoint(eRender, eConsole)

            # Activate IAudioClient
            audio_client_ptr = device.Activate(
                ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None
            )
            audio_client = ctypes.cast(
                audio_client_ptr, ctypes.POINTER(IAudioClient)
            )

            # Get mix format
            mix_fmt_ptr = audio_client.GetMixFormat()
            mix_fmt = ctypes.cast(mix_fmt_ptr, ctypes.POINTER(WAVEFORMATEX)).contents
            self._sample_rate = mix_fmt.nSamplesPerSec
            self._channels = mix_fmt.nChannels
            self._bits_per_sample = mix_fmt.wBitsPerSample

            log.info(
                "Audio format: %d Hz, %d ch, %d bit",
                self._sample_rate,
                self._channels,
                self._bits_per_sample,
            )
            self._format_event.set()

            # Initialize in loopback mode
            buffer_duration = REFTIMES_PER_SEC  # 1 second buffer
            audio_client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK,
                buffer_duration,
                0,
                mix_fmt_ptr,
                None,
            )

            buffer_size = audio_client.GetBufferSize()

            # Get capture client service
            capture_ptr = audio_client.GetService(
                ctypes.byref(IID_IAudioCaptureClient)
            )
            capture_client = ctypes.cast(
                capture_ptr, ctypes.POINTER(IAudioCaptureClient)
            )

            audio_client.Start()
            log.info("WASAPI loopback capture started")

            bytes_per_frame = self._channels * (self._bits_per_sample // 8)
            sleep_interval = buffer_size / self._sample_rate / 2

            try:
                while not self._stop_event.is_set():
                    time.sleep(sleep_interval)
                    self._drain_packets(capture_client, bytes_per_frame)
                # Final drain
                self._drain_packets(capture_client, bytes_per_frame)
            finally:
                audio_client.Stop()
                audio_client.Reset()
                log.info("WASAPI loopback capture stopped")
        finally:
            comtypes.CoUninitialize()

    def _drain_packets(self, capture_client, bytes_per_frame: int) -> None:
        """Read all available packets from the capture client."""
        AUDCLNT_BUFFERFLAGS_SILENT = 0x2
        while True:
            packet_size = capture_client.GetNextPacketSize()
            if packet_size == 0:
                break

            data_ptr, num_frames, flags, _, _ = capture_client.GetBuffer()
            if num_frames > 0:
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    # Write silence
                    silence = b"\x00" * (num_frames * bytes_per_frame)
                    try:
                        self._stream.write(silence)
                    except (BrokenPipeError, OSError):
                        self._stop_event.set()
                        capture_client.ReleaseBuffer(num_frames)
                        return
                else:
                    size = num_frames * bytes_per_frame
                    buf = (ctypes.c_char * size).from_address(data_ptr)
                    try:
                        self._stream.write(bytes(buf))
                    except (BrokenPipeError, OSError):
                        self._stop_event.set()
                        capture_client.ReleaseBuffer(num_frames)
                        return
            capture_client.ReleaseBuffer(num_frames)

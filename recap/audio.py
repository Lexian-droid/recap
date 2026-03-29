"""recap – WASAPI loopback audio capture to WAV file.

Captures system audio (loopback) using the Windows Audio Session API and
writes PCM data directly to a temporary WAV file.

The heavy lifting happens in a background thread so the main thread can
coordinate video capture separately.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Optional

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


def _float32_to_int16(data: bytes) -> bytes:
    """Convert raw float32 LE PCM bytes to int16 LE PCM bytes."""
    n = len(data) // 4
    floats = struct.unpack_from(f'<{n}f', data)
    return struct.pack(
        f'<{n}h',
        *(max(-32768, min(32767, int(f * 32767))) for f in floats),
    )


class AudioCapture:
    """WASAPI loopback capture of system audio to a WAV file."""

    def __init__(
        self,
        wav_path: str | Path,
        process_id: Optional[int] = None,
    ) -> None:
        self._wav_path = str(wav_path)
        self._process_id = process_id
        self._wav_file = None
        self._is_float = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._format_event = threading.Event()
        self._started_event = threading.Event()
        self._started_at: Optional[float] = None
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
        self._format_event.clear()
        self._started_event.clear()
        self._started_at = None
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

    def wait_started(self, timeout: float = 10.0) -> bool:
        """Block until the WASAPI client has started capturing."""
        return self._started_event.wait(timeout=timeout)

    @property
    def started_at(self) -> Optional[float]:
        """Monotonic timestamp when capture entered running state."""
        return self._started_at

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
            # GetMixFormat is NOT implemented on process-loopback IAudioClients.
            # Always obtain the mix format from the default render endpoint first.
            enumerator = comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator
            )
            device = enumerator.GetDefaultAudioEndpoint(eRender, eConsole)
            fmt_client_ptr = device.Activate(
                ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None
            )
            fmt_client = ctypes.cast(
                fmt_client_ptr, ctypes.POINTER(IAudioClient)
            )
            mix_fmt_ptr = fmt_client.GetMixFormat()

            if self._process_id is not None:
                log.info(
                    "Process-specific audio loopback for PID %d",
                    self._process_id,
                )
                audio_client = self._activate_process_loopback(
                    self._process_id, IAudioClient, IID_IAudioClient
                )
            else:
                audio_client = fmt_client

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
            self._started_at = time.perf_counter()
            self._started_event.set()
            log.info("WASAPI loopback capture started")

            self._is_float = (self._bits_per_sample == 32)
            bytes_per_frame = self._channels * (self._bits_per_sample // 8)
            # Poll more frequently to reduce clock drift relative to video.
            # Target 10x per typical frame interval (e.g., ~1.6ms @ 30fps).
            sleep_interval = min(0.002, buffer_size / self._sample_rate / 10)

            self._wav_file = wave.open(self._wav_path, 'wb')
            self._wav_file.setnchannels(self._channels)
            self._wav_file.setsampwidth(2)  # always write int16
            self._wav_file.setframerate(self._sample_rate)

            try:
                while not self._stop_event.is_set():
                    time.sleep(sleep_interval)
                    self._drain_packets(capture_client, bytes_per_frame)
                # Final drain
                self._drain_packets(capture_client, bytes_per_frame)
            finally:
                if self._wav_file is not None:
                    self._wav_file.close()
                    self._wav_file = None
                audio_client.Stop()
                audio_client.Reset()
                log.info("WASAPI loopback capture stopped")
        finally:
            comtypes.CoUninitialize()

    def _drain_packets(self, capture_client, bytes_per_frame: int) -> None:
        """Read all available packets from the capture client."""
        AUDCLNT_BUFFERFLAGS_SILENT = 0x2
        int16_frame_size = self._channels * 2
        while True:
            packet_size = capture_client.GetNextPacketSize()
            if packet_size == 0:
                break

            data_ptr, num_frames, flags, _, _ = capture_client.GetBuffer()
            if num_frames > 0:
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    silence = b"\x00" * (num_frames * int16_frame_size)
                    self._wav_file.writeframes(silence)
                else:
                    size = num_frames * bytes_per_frame
                    buf = (ctypes.c_char * size).from_address(data_ptr)
                    raw = bytes(buf)
                    if self._is_float:
                        raw = _float32_to_int16(raw)
                    self._wav_file.writeframes(raw)
            capture_client.ReleaseBuffer(num_frames)

    # ------------------------------------------------------------------
    # Process-specific loopback activation
    # ------------------------------------------------------------------

    def _activate_process_loopback(self, process_id: int, IAudioClient, IID_IAudioClient):
        """Activate a process-loopback IAudioClient for *process_id*.

        Uses ``ActivateAudioInterfaceAsync`` with
        ``AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`` to capture audio
        only from the given process and its child processes.

        Returns a ``ctypes.POINTER(IAudioClient)`` ready for
        ``GetMixFormat`` / ``Initialize``.

        Requires Windows 10 version 2004 (build 19041) or later.
        """
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

        # ---------- structures ----------

        class _LoopbackParams(ctypes.Structure):
            _fields_ = [
                ("TargetProcessId", ctypes.wintypes.DWORD),
                ("ProcessLoopbackMode", ctypes.c_uint),
            ]

        class _ActivationParams(ctypes.Structure):
            _fields_ = [
                ("ActivationType", ctypes.c_uint),
                ("ProcessLoopbackParams", _LoopbackParams),
            ]

        class _BLOB(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("pBlobData", ctypes.c_void_p),
            ]

        class _PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("reserved1", ctypes.c_ushort),
                ("reserved2", ctypes.c_ushort),
                ("reserved3", ctypes.c_ushort),
                ("blob", _BLOB),
            ]

        act_params = _ActivationParams(
            ActivationType=1,  # AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            ProcessLoopbackParams=_LoopbackParams(
                TargetProcessId=process_id,
                ProcessLoopbackMode=0,  # PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
            ),
        )
        prop_var = _PROPVARIANT(
            vt=0x0041,  # VT_BLOB
            blob=_BLOB(
                cbSize=ctypes.sizeof(act_params),
                pBlobData=ctypes.addressof(act_params),
            ),
        )

        # ---------- minimal vtable COM object for completion handler ----------

        done_event = threading.Event()
        result: dict = {"hr": -1, "unk": None}
        ptr_size = ctypes.sizeof(ctypes.c_void_p)

        # Function types for the vtable
        _QI_t = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        _AR_t = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        _RE_t = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        _AC_t = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p
        )
        # IActivateAudioInterfaceAsyncOperation::GetActivateResult (vtable slot 3)
        _GAR_t = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.HRESULT),
            ctypes.POINTER(ctypes.c_void_p),
        )

        def _qi(this, riid, ppv):
            ppv[0] = this
            return 0  # S_OK

        def _ar(this):
            return 1

        def _re(this):
            return 1

        def _ac(this, op_ptr):
            try:
                if not op_ptr:
                    result["hr"] = -1
                    return 0
                # Read vtable pointer from the async-operation COM object
                vtbl = ctypes.c_void_p.from_address(op_ptr).value
                if not vtbl:
                    result["hr"] = -1
                    return 0
                # GetActivateResult is at vtable slot 3
                fn_ptr = ctypes.c_void_p.from_address(
                    vtbl + 3 * ptr_size
                ).value
                get_result = _GAR_t(fn_ptr)
                hr_out = ctypes.HRESULT(0)
                unk_out = ctypes.c_void_p(0)
                get_result(op_ptr, ctypes.byref(hr_out), ctypes.byref(unk_out))
                result["hr"] = hr_out.value
                result["unk"] = unk_out.value
            except Exception as exc:
                log.error(
                    "Process loopback ActivateCompleted error: %s",
                    exc,
                    exc_info=True,
                )
                result["hr"] = -1
            finally:
                done_event.set()
            return 0  # S_OK

        class _VTBL(ctypes.Structure):
            _fields_ = [
                ("qi", _QI_t),
                ("ar", _AR_t),
                ("re", _RE_t),
                ("ac", _AC_t),
            ]

        class _OBJ(ctypes.Structure):
            _fields_ = [("vtbl", ctypes.POINTER(_VTBL))]

        vtbl_inst = _VTBL(_QI_t(_qi), _AR_t(_ar), _RE_t(_re), _AC_t(_ac))
        obj_inst = _OBJ(ctypes.pointer(vtbl_inst))

        # Keep all objects alive until the async completion fires
        _keep_alive = (act_params, prop_var, vtbl_inst, obj_inst)

        # ---------- call ActivateAudioInterfaceAsync ----------

        mmdevapi = ctypes.windll.mmdevapi
        mmdevapi.ActivateAudioInterfaceAsync.restype = ctypes.HRESULT
        mmdevapi.ActivateAudioInterfaceAsync.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_void_p,                     # REFIID
            ctypes.c_void_p,                     # PROPVARIANT*
            ctypes.c_void_p,                     # IActivateAudioInterfaceCompletionHandler*
            ctypes.POINTER(ctypes.c_void_p),     # IActivateAudioInterfaceAsyncOperation**
        ]

        async_op = ctypes.c_void_p(0)
        hr = mmdevapi.ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            ctypes.addressof(IID_IAudioClient),
            ctypes.addressof(prop_var),
            ctypes.c_void_p(ctypes.addressof(obj_inst)),
            ctypes.byref(async_op),
        )
        if hr != 0:
            raise AudioCaptureError(
                f"ActivateAudioInterfaceAsync failed: {hr & 0xFFFFFFFF:#010x}. "
                "Process-specific audio capture requires Windows 10 "
                "version 2004 (build 19041) or later."
            )

        if not done_event.wait(timeout=15.0):
            raise AudioCaptureError(
                "Process audio loopback activation timed out."
            )

        # Silence the "unused variable" lint warning while keeping objects alive
        _ = _keep_alive

        if result["hr"] is not None and result["hr"] < 0:
            raise AudioCaptureError(
                f"GetActivateResult failed: {result['hr'] & 0xFFFFFFFF:#010x}"
            )
        if not result["unk"]:
            raise AudioCaptureError(
                "Process audio activation returned a null interface pointer."
            )

        # ---------- QueryInterface IUnknown* -> IAudioClient* ----------

        _QI_unk_t = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        unk_raw = result["unk"]
        vtbl_base = ctypes.c_void_p.from_address(unk_raw).value
        qi_fn = ctypes.c_void_p.from_address(vtbl_base).value
        qi = _QI_unk_t(qi_fn)

        audio_client_raw = ctypes.c_void_p(0)
        hr_qi = qi(
            unk_raw,
            ctypes.addressof(IID_IAudioClient),
            ctypes.byref(audio_client_raw),
        )
        if hr_qi != 0:
            raise AudioCaptureError(
                f"QueryInterface(IAudioClient) failed: {hr_qi & 0xFFFFFFFF:#010x}"
            )
        if not audio_client_raw.value:
            raise AudioCaptureError(
                "QueryInterface(IAudioClient) returned a null pointer."
            )

        return ctypes.cast(audio_client_raw, ctypes.POINTER(IAudioClient))

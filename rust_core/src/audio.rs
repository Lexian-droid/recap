use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Instant;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use windows::Win32::Media::Audio::{
    eConsole, eRender, IAudioCaptureClient, IAudioClient, IMMDeviceEnumerator,
    MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_SHAREMODE_SHARED,
    AUDCLNT_STREAMFLAGS_LOOPBACK,
};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL,
    COINIT_MULTITHREADED,
};

const REFTIMES_PER_SEC: i64 = 10_000_000;

struct AudioState {
    sample_rate: Mutex<u32>,
    channels: Mutex<u16>,
    bits_per_sample: Mutex<u16>,
    running: AtomicBool,
    stop: AtomicBool,
    format_ready: (Mutex<bool>, Condvar),
    started: (Mutex<bool>, Condvar),
    started_at: Mutex<Option<f64>>,
    error: Mutex<Option<String>>,
}

#[pyclass]
pub struct AudioCapture {
    state: Arc<AudioState>,
    wav_path: String,
    process_id: Option<u32>,
    thread_handle: Mutex<Option<thread::JoinHandle<()>>>,
}

#[pymethods]
impl AudioCapture {
    #[new]
    #[pyo3(signature = (wav_path, process_id=None))]
    fn new(wav_path: String, process_id: Option<u32>) -> Self {
        let state = Arc::new(AudioState {
            sample_rate: Mutex::new(48000),
            channels: Mutex::new(2),
            bits_per_sample: Mutex::new(16),
            running: AtomicBool::new(false),
            stop: AtomicBool::new(false),
            format_ready: (Mutex::new(false), Condvar::new()),
            started: (Mutex::new(false), Condvar::new()),
            started_at: Mutex::new(None),
            error: Mutex::new(None),
        });
        AudioCapture {
            state,
            wav_path,
            process_id,
            thread_handle: Mutex::new(None),
        }
    }

    #[getter]
    fn sample_rate(&self) -> u32 {
        *self.state.sample_rate.lock().unwrap()
    }

    #[getter]
    fn channels(&self) -> u16 {
        *self.state.channels.lock().unwrap()
    }

    #[getter]
    fn bits_per_sample(&self) -> u16 {
        *self.state.bits_per_sample.lock().unwrap()
    }

    fn start(&self) -> PyResult<()> {
        if self.state.running.load(Ordering::SeqCst) {
            return Ok(());
        }
        self.state.stop.store(false, Ordering::SeqCst);
        {
            let mut fmt = self.state.format_ready.0.lock().unwrap();
            *fmt = false;
        }
        {
            let mut started = self.state.started.0.lock().unwrap();
            *started = false;
        }
        *self.state.started_at.lock().unwrap() = None;

        self.state.running.store(true, Ordering::SeqCst);

        let state = Arc::clone(&self.state);
        let wav_path = self.wav_path.clone();
        let process_id = self.process_id;

        let handle = thread::Builder::new()
            .name("recap-audio-rs".into())
            .spawn(move || {
                audio_capture_thread(state, wav_path, process_id);
            })
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to spawn audio thread: {}", e)))?;

        *self.thread_handle.lock().unwrap() = Some(handle);
        Ok(())
    }

    fn stop(&self) {
        self.state.stop.store(true, Ordering::SeqCst);
        self.state.running.store(false, Ordering::SeqCst);
    }

    #[pyo3(signature = (timeout=None))]
    fn wait(&self, timeout: Option<f64>) {
        let handle = self.thread_handle.lock().unwrap().take();
        if let Some(h) = handle {
            if let Some(t) = timeout {
                let dur = std::time::Duration::from_secs_f64(t);
                let start = Instant::now();
                loop {
                    if h.is_finished() {
                        let _ = h.join();
                        return;
                    }
                    if start.elapsed() >= dur {
                        return;
                    }
                    thread::sleep(std::time::Duration::from_millis(10));
                }
            } else {
                let _ = h.join();
            }
        }
    }

    #[pyo3(signature = (timeout=10.0))]
    fn wait_format_ready(&self, timeout: f64) -> bool {
        let (lock, cvar) = &self.state.format_ready;
        let guard = lock.lock().unwrap();
        let dur = std::time::Duration::from_secs_f64(timeout);
        let result = cvar
            .wait_timeout_while(guard, dur, |ready| !*ready)
            .unwrap();
        *result.0
    }

    #[pyo3(signature = (timeout=10.0))]
    fn wait_started(&self, timeout: f64) -> bool {
        let (lock, cvar) = &self.state.started;
        let guard = lock.lock().unwrap();
        let dur = std::time::Duration::from_secs_f64(timeout);
        let result = cvar
            .wait_timeout_while(guard, dur, |started| !*started)
            .unwrap();
        *result.0
    }

    #[getter]
    fn started_at(&self) -> Option<f64> {
        *self.state.started_at.lock().unwrap()
    }
}

fn float32_to_int16(data: &[u8]) -> Vec<u8> {
    let n = data.len() / 4;
    let mut out = Vec::with_capacity(n * 2);
    for i in 0..n {
        let bytes = [data[i * 4], data[i * 4 + 1], data[i * 4 + 2], data[i * 4 + 3]];
        let f = f32::from_le_bytes(bytes);
        let sample = (f * 32767.0).clamp(-32768.0, 32767.0) as i16;
        out.extend_from_slice(&sample.to_le_bytes());
    }
    out
}

fn audio_capture_thread(state: Arc<AudioState>, wav_path: String, process_id: Option<u32>) {
    if let Err(e) = audio_capture_impl(&state, &wav_path, process_id) {
        let mut err = state.error.lock().unwrap();
        *err = Some(e);
    }
}

fn audio_capture_impl(
    state: &AudioState,
    wav_path: &str,
    _process_id: Option<u32>,
) -> Result<(), String> {
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);

        let result = audio_capture_core(state, wav_path);

        CoUninitialize();

        result
    }
}

unsafe fn audio_capture_core(
    state: &AudioState,
    wav_path: &str,
) -> Result<(), String> {
    let enumerator: IMMDeviceEnumerator =
        CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
            .map_err(|e| format!("CoCreateInstance(MMDeviceEnumerator): {}", e))?;

    let device = enumerator
        .GetDefaultAudioEndpoint(eRender, eConsole)
        .map_err(|e| format!("GetDefaultAudioEndpoint: {}", e))?;

    let audio_client: IAudioClient = device
        .Activate(CLSCTX_ALL, None)
        .map_err(|e| format!("Activate(IAudioClient): {}", e))?;

    let mix_fmt_ptr = audio_client
        .GetMixFormat()
        .map_err(|e| format!("GetMixFormat: {}", e))?;
    let mix_fmt = &*mix_fmt_ptr;

    let sample_rate = mix_fmt.nSamplesPerSec;
    let channels = mix_fmt.nChannels;
    let bits_per_sample = mix_fmt.wBitsPerSample;

    *state.sample_rate.lock().unwrap() = sample_rate;
    *state.channels.lock().unwrap() = channels;
    *state.bits_per_sample.lock().unwrap() = bits_per_sample;

    // Signal format ready
    {
        let mut rdy = state.format_ready.0.lock().unwrap();
        *rdy = true;
        state.format_ready.1.notify_all();
    }

    // Initialize in loopback mode
    audio_client
        .Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            REFTIMES_PER_SEC,
            0,
            mix_fmt_ptr,
            None,
        )
        .map_err(|e| format!("IAudioClient::Initialize: {}", e))?;

    let buffer_size = audio_client
        .GetBufferSize()
        .map_err(|e| format!("GetBufferSize: {}", e))?;

    let capture_client: IAudioCaptureClient = audio_client
        .GetService()
        .map_err(|e| format!("GetService(IAudioCaptureClient): {}", e))?;

    audio_client
        .Start()
        .map_err(|e| format!("IAudioClient::Start: {}", e))?;

    *state.started_at.lock().unwrap() = Some(0.0);
    {
        let mut s = state.started.0.lock().unwrap();
        *s = true;
        state.started.1.notify_all();
    }

    let is_float = bits_per_sample == 32;
    let bytes_per_frame = channels as usize * (bits_per_sample as usize / 8);
    let sleep_interval = std::time::Duration::from_secs_f64(
        (buffer_size as f64 / sample_rate as f64 / 10.0).min(0.002),
    );

    // Open WAV file
    let wav_file = std::fs::File::create(wav_path)
        .map_err(|e| format!("Failed to create WAV file: {}", e))?;
    let mut writer = WavWriter::new(wav_file, channels, sample_rate);
    writer.write_header().map_err(|e| format!("WAV header: {}", e))?;

    let int16_frame_size = channels as usize * 2;

    let drain = |writer: &mut WavWriter| -> Result<(), String> {
        loop {
            let packet_size = capture_client
                .GetNextPacketSize()
                .map_err(|e| format!("GetNextPacketSize: {}", e))?;
            if packet_size == 0 {
                break;
            }

            let mut data_ptr: *mut u8 = std::ptr::null_mut();
            let mut num_frames: u32 = 0;
            let mut flags: u32 = 0;
            let mut device_pos: u64 = 0;
            let mut qpc_pos: u64 = 0;

            capture_client
                .GetBuffer(
                    &mut data_ptr,
                    &mut num_frames,
                    &mut flags,
                    Some(&mut device_pos),
                    Some(&mut qpc_pos),
                )
                .map_err(|e| format!("GetBuffer: {}", e))?;

            if num_frames > 0 {
                if flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 {
                    let silence = vec![0u8; num_frames as usize * int16_frame_size];
                    writer
                        .write_data(&silence)
                        .map_err(|e| format!("WAV write: {}", e))?;
                } else {
                    let size = num_frames as usize * bytes_per_frame;
                    let raw = std::slice::from_raw_parts(data_ptr, size);
                    if is_float {
                        let converted = float32_to_int16(raw);
                        writer
                            .write_data(&converted)
                            .map_err(|e| format!("WAV write: {}", e))?;
                    } else {
                        writer
                            .write_data(raw)
                            .map_err(|e| format!("WAV write: {}", e))?;
                    }
                }
            }

            capture_client
                .ReleaseBuffer(num_frames)
                .map_err(|e| format!("ReleaseBuffer: {}", e))?;
        }
        Ok(())
    };

    while !state.stop.load(Ordering::SeqCst) {
        thread::sleep(sleep_interval);
        drain(&mut writer)?;
    }
    // Final drain
    drain(&mut writer)?;

    audio_client
        .Stop()
        .map_err(|e| format!("IAudioClient::Stop: {}", e))?;
    audio_client
        .Reset()
        .map_err(|e| format!("IAudioClient::Reset: {}", e))?;

    writer.finalize().map_err(|e| format!("WAV finalize: {}", e))?;

    Ok(())
}

// ── Simple WAV writer ────────────────────────────────────────────

struct WavWriter {
    file: std::fs::File,
    channels: u16,
    sample_rate: u32,
    data_bytes: u32,
}

impl WavWriter {
    fn new(file: std::fs::File, channels: u16, sample_rate: u32) -> Self {
        WavWriter {
            file,
            channels,
            sample_rate,
            data_bytes: 0,
        }
    }

    fn write_header(&mut self) -> std::io::Result<()> {
        use std::io::Write;
        let bits_per_sample: u16 = 16; // always int16 output
        let byte_rate = self.sample_rate * self.channels as u32 * 2;
        let block_align = self.channels * 2;

        // RIFF header (placeholder sizes)
        self.file.write_all(b"RIFF")?;
        self.file.write_all(&0u32.to_le_bytes())?;
        self.file.write_all(b"WAVE")?;

        // fmt chunk
        self.file.write_all(b"fmt ")?;
        self.file.write_all(&16u32.to_le_bytes())?;
        self.file.write_all(&1u16.to_le_bytes())?; // PCM
        self.file.write_all(&self.channels.to_le_bytes())?;
        self.file.write_all(&self.sample_rate.to_le_bytes())?;
        self.file.write_all(&byte_rate.to_le_bytes())?;
        self.file.write_all(&block_align.to_le_bytes())?;
        self.file.write_all(&bits_per_sample.to_le_bytes())?;

        // data chunk header
        self.file.write_all(b"data")?;
        self.file.write_all(&0u32.to_le_bytes())?;

        Ok(())
    }

    fn write_data(&mut self, data: &[u8]) -> std::io::Result<()> {
        use std::io::Write;
        self.file.write_all(data)?;
        self.data_bytes += data.len() as u32;
        Ok(())
    }

    fn finalize(&mut self) -> std::io::Result<()> {
        use std::io::{Seek, SeekFrom, Write};
        let file_size = 36 + self.data_bytes;
        // Patch RIFF size
        self.file.seek(SeekFrom::Start(4))?;
        self.file.write_all(&file_size.to_le_bytes())?;
        // Patch data size
        self.file.seek(SeekFrom::Start(40))?;
        self.file.write_all(&self.data_bytes.to_le_bytes())?;
        self.file.flush()?;
        Ok(())
    }
}

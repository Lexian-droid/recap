use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Instant;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::Graphics::Gdi::{
    BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject,
    GetDC, GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER,
    BI_RGB, DIB_RGB_COLORS, SRCCOPY,
};
use windows::Win32::Storage::Xps::{PrintWindow, PRINT_WINDOW_FLAGS};
use windows::Win32::UI::WindowsAndMessaging::{GetClientRect, IsWindow};

const PW_RENDERFULLCONTENT: PRINT_WINDOW_FLAGS = PRINT_WINDOW_FLAGS(0x00000002);

struct CaptureState {
    width: Mutex<i32>,
    height: Mutex<i32>,
    running: AtomicBool,
    stop: AtomicBool,
    ready_signal: (Mutex<bool>, Condvar),
    error: Mutex<Option<String>>,
}

#[pyclass]
pub struct VideoCapture {
    state: Arc<CaptureState>,
    thread_handle: Mutex<Option<thread::JoinHandle<()>>>,
    fps: i32,
    monitor_index: Option<i32>,
    window_handle: Option<isize>,
    stream: PyObject,
}

#[pymethods]
impl VideoCapture {
    #[new]
    #[pyo3(signature = (output_stream, *, fps=30, monitor_index=None, window_handle=None))]
    fn new(
        output_stream: PyObject,
        fps: i32,
        monitor_index: Option<i32>,
        window_handle: Option<isize>,
    ) -> Self {
        let state = Arc::new(CaptureState {
            width: Mutex::new(0),
            height: Mutex::new(0),
            running: AtomicBool::new(false),
            stop: AtomicBool::new(false),
            ready_signal: (Mutex::new(false), Condvar::new()),
            error: Mutex::new(None),
        });
        VideoCapture {
            state,
            thread_handle: Mutex::new(None),
            fps,
            monitor_index,
            window_handle,
            stream: output_stream,
        }
    }

    #[staticmethod]
    #[pyo3(signature = (monitor_index=None, window_handle=None, target_fps=60))]
    fn measure_achievable_fps(
        monitor_index: Option<i32>,
        window_handle: Option<isize>,
        target_fps: i32,
    ) -> PyResult<i32> {
        measure_fps_impl(monitor_index, window_handle, target_fps)
    }

    #[getter]
    fn width(&self) -> i32 {
        *self.state.width.lock().unwrap()
    }

    #[getter]
    fn height(&self) -> i32 {
        *self.state.height.lock().unwrap()
    }

    fn start(&self, py: Python<'_>) -> PyResult<()> {
        if self.state.running.load(Ordering::SeqCst) {
            return Ok(());
        }
        self.state.stop.store(false, Ordering::SeqCst);
        {
            let mut rdy = self.state.ready_signal.0.lock().unwrap();
            *rdy = false;
        }

        self.state.running.store(true, Ordering::SeqCst);

        let state = Arc::clone(&self.state);
        let fps = self.fps;
        let monitor_index = self.monitor_index;
        let window_handle = self.window_handle;
        let stream = self.stream.clone_ref(py);

        let handle = thread::Builder::new()
            .name("recap-video-rs".into())
            .spawn(move || {
                capture_thread(state, fps, monitor_index, window_handle, stream);
            })
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to spawn thread: {}", e)))?;

        *self.thread_handle.lock().unwrap() = Some(handle);
        Ok(())
    }

    #[pyo3(signature = (timeout=10.0))]
    fn wait_ready(&self, timeout: f64) -> bool {
        let (lock, cvar) = &self.state.ready_signal;
        let guard = lock.lock().unwrap();
        let dur = std::time::Duration::from_secs_f64(timeout);
        let result = cvar
            .wait_timeout_while(guard, dur, |ready| !*ready)
            .unwrap();
        *result.0
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
}

fn capture_thread(
    state: Arc<CaptureState>,
    fps: i32,
    monitor_index: Option<i32>,
    window_handle: Option<isize>,
    stream: PyObject,
) {
    let result = if let Some(hwnd) = window_handle {
        capture_window(&state, fps, hwnd, &stream)
    } else {
        capture_monitor(&state, fps, monitor_index.unwrap_or(0), &stream)
    };

    if let Err(e) = result {
        let mut err = state.error.lock().unwrap();
        *err = Some(e);
    }
}

fn write_to_stream(stream: &PyObject, data: &[u8]) -> Result<(), String> {
    Python::with_gil(|py| {
        let bytes = PyBytes::new(py, data);
        stream
            .call_method1(py, "write", (bytes,))
            .map_err(|e| {
                let msg = e.to_string();
                if msg.contains("BrokenPipe") || msg.contains("OSError") {
                    "pipe_closed".to_string()
                } else {
                    msg
                }
            })?;
        Ok(())
    })
}

fn wait_stream_ready(stream: &PyObject) -> Result<bool, String> {
    Python::with_gil(|py| {
        let has_method = stream.getattr(py, "wait_ready").is_ok();
        if has_method {
            let result = stream
                .call_method1(py, "wait_ready", (30.0f64,))
                .map_err(|e| e.to_string())?;
            let ready = result.extract::<bool>(py).unwrap_or(false);
            Ok(ready)
        } else {
            Ok(true)
        }
    })
}

fn capture_monitor(
    state: &Arc<CaptureState>,
    fps: i32,
    idx: i32,
    stream: &PyObject,
) -> Result<(), String> {
    let monitors = super::discovery::list_monitors()
        .map_err(|e| e.to_string())?;

    if idx as usize >= monitors.len() {
        return Err(format!(
            "Monitor index {} not found (have {}).",
            idx,
            monitors.len()
        ));
    }
    let mon = &monitors[idx as usize];
    *state.width.lock().unwrap() = mon.width;
    *state.height.lock().unwrap() = mon.height;

    // Signal ready
    {
        let mut rdy = state.ready_signal.0.lock().unwrap();
        *rdy = true;
        state.ready_signal.1.notify_all();
    }

    // Wait for the stream/pipe to be ready
    if !wait_stream_ready(stream).map_err(|e| e)? {
        return Err("Timed out waiting for video pipe.".into());
    }

    let width = mon.width;
    let height = mon.height;
    let mon_x = mon.x;
    let mon_y = mon.y;

    unsafe {
        let hdc_screen = GetDC(None);
        let hdc_mem = CreateCompatibleDC(Some(hdc_screen));
        let hbm = CreateCompatibleBitmap(hdc_screen, width, height);
        let old_bm = SelectObject(hdc_mem, hbm.into());

        let mut bmi = make_bitmapinfo(width, height);
        let buf_size = (width * height * 4) as usize;
        let mut buf: Vec<u8> = vec![0u8; buf_size];
        let frame_interval =
            std::time::Duration::from_secs_f64(1.0 / fps as f64);
        let mut next_frame: Option<Instant> = None;

        let result: Result<(), String> = (|| {
            while !state.stop.load(Ordering::SeqCst) {
                let _ = BitBlt(
                    hdc_mem,
                    0,
                    0,
                    width,
                    height,
                    Some(hdc_screen),
                    mon_x,
                    mon_y,
                    SRCCOPY,
                );
                GetDIBits(
                    hdc_mem,
                    hbm,
                    0,
                    height as u32,
                    Some(buf.as_mut_ptr() as *mut _),
                    &mut bmi,
                    DIB_RGB_COLORS,
                );

                match write_to_stream(stream, &buf) {
                    Ok(()) => {}
                    Err(ref e) if e == "pipe_closed" => break,
                    Err(e) => return Err(e),
                }

                let now = Instant::now();
                if next_frame.is_none() {
                    next_frame = Some(now);
                }
                let nf = next_frame.as_mut().unwrap();
                *nf += frame_interval;
                if *nf > now {
                    thread::sleep(*nf - now);
                }
            }
            Ok(())
        })();

        SelectObject(hdc_mem, old_bm);
        let _ = DeleteObject(hbm.into());
        let _ = DeleteDC(hdc_mem);
        ReleaseDC(None, hdc_screen);

        result
    }
}

fn capture_window(
    state: &Arc<CaptureState>,
    fps: i32,
    hwnd_val: isize,
    stream: &PyObject,
) -> Result<(), String> {
    let hwnd = HWND(hwnd_val as *mut _);

    unsafe {
        if !IsWindow(Some(hwnd)).as_bool() {
            return Err(format!("HWND {:#x} is not a valid window.", hwnd_val));
        }

        let mut rect = RECT::default();
        let _ = GetClientRect(hwnd, &mut rect);
        let width = rect.right - rect.left;
        let height = rect.bottom - rect.top;

        if width <= 0 || height <= 0 {
            return Err(format!(
                "Window has no client area ({}x{}). It may be minimised.",
                width, height
            ));
        }

        *state.width.lock().unwrap() = width;
        *state.height.lock().unwrap() = height;

        // Signal ready
        {
            let mut rdy = state.ready_signal.0.lock().unwrap();
            *rdy = true;
            state.ready_signal.1.notify_all();
        }

        // Wait for the stream/pipe to be ready
        if !wait_stream_ready(stream).map_err(|e| e)? {
            return Err("Timed out waiting for video pipe.".into());
        }

        let hdc_window = GetDC(Some(hwnd));
        let hdc_mem = CreateCompatibleDC(Some(hdc_window));
        let hbm = CreateCompatibleBitmap(hdc_window, width, height);
        let old_bm = SelectObject(hdc_mem, hbm.into());

        let mut bmi = make_bitmapinfo(width, height);
        let buf_size = (width * height * 4) as usize;
        let mut buf: Vec<u8> = vec![0u8; buf_size];
        let frame_interval =
            std::time::Duration::from_secs_f64(1.0 / fps as f64);
        let mut next_frame: Option<Instant> = None;

        let result: Result<(), String> = (|| {
            while !state.stop.load(Ordering::SeqCst) {
                let _ = PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT);
                GetDIBits(
                    hdc_mem,
                    hbm,
                    0,
                    height as u32,
                    Some(buf.as_mut_ptr() as *mut _),
                    &mut bmi,
                    DIB_RGB_COLORS,
                );

                match write_to_stream(stream, &buf) {
                    Ok(()) => {}
                    Err(ref e) if e == "pipe_closed" => break,
                    Err(e) => return Err(e),
                }

                let now = Instant::now();
                if next_frame.is_none() {
                    next_frame = Some(now);
                }
                let nf = next_frame.as_mut().unwrap();
                *nf += frame_interval;
                if *nf > now {
                    thread::sleep(*nf - now);
                }
            }
            Ok(())
        })();

        SelectObject(hdc_mem, old_bm);
        let _ = DeleteObject(hbm.into());
        let _ = DeleteDC(hdc_mem);
        ReleaseDC(Some(hwnd), hdc_window);

        result
    }
}

fn measure_fps_impl(
    monitor_index: Option<i32>,
    window_handle: Option<isize>,
    target_fps: i32,
) -> PyResult<i32> {
    let width: i32;
    let height: i32;
    let use_printwindow: bool;
    let source_x: i32;
    let source_y: i32;

    let monitors;
    let hwnd;

    if let Some(h) = window_handle {
        hwnd = HWND(h as *mut _);
        unsafe {
            let mut rect = RECT::default();
            let _ = GetClientRect(hwnd, &mut rect);
            width = rect.right - rect.left;
            height = rect.bottom - rect.top;
        }
        use_printwindow = true;
        source_x = 0;
        source_y = 0;
    } else {
        hwnd = HWND::default();
        monitors = super::discovery::list_monitors()
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        let idx = monitor_index.unwrap_or(0) as usize;
        if idx >= monitors.len() {
            return Ok(target_fps);
        }
        let mon = &monitors[idx];
        width = mon.width;
        height = mon.height;
        use_printwindow = false;
        source_x = mon.x;
        source_y = mon.y;
    };

    unsafe {
        let hdc_source = if use_printwindow {
            GetDC(Some(hwnd))
        } else {
            GetDC(None)
        };
        let hdc_mem = CreateCompatibleDC(Some(hdc_source));
        let hbm = CreateCompatibleBitmap(hdc_source, width, height);
        let old_bm = SelectObject(hdc_mem, hbm.into());

        let mut bmi = make_bitmapinfo(width, height);
        let buf_size = (width * height * 4) as usize;
        let mut buf: Vec<u8> = vec![0u8; buf_size];

        let mut timings: Vec<f64> = Vec::with_capacity(10);
        for _ in 0..10 {
            let t0 = Instant::now();
            if use_printwindow {
                let _ = PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT);
            } else {
                let _ = BitBlt(
                    hdc_mem,
                    0,
                    0,
                    width,
                    height,
                    Some(hdc_source),
                    source_x,
                    source_y,
                    SRCCOPY,
                );
            }
            GetDIBits(
                hdc_mem,
                hbm,
                0,
                height as u32,
                Some(buf.as_mut_ptr() as *mut _),
                &mut bmi,
                DIB_RGB_COLORS,
            );
            timings.push(t0.elapsed().as_secs_f64());
        }

        SelectObject(hdc_mem, old_bm);
        let _ = DeleteObject(hbm.into());
        let _ = DeleteDC(hdc_mem);
        let release_hwnd = if use_printwindow {
            Some(hwnd)
        } else {
            None
        };
        ReleaseDC(release_hwnd, hdc_source);

        // Average of timings after skipping first 3 warmup
        let avg_time: f64 =
            timings[3..].iter().sum::<f64>() / (timings.len() - 3) as f64;
        let achievable_fps = std::cmp::max(15, (1.0 / avg_time) as i32);

        if achievable_fps as f64 >= target_fps as f64 * 0.9 {
            Ok(target_fps)
        } else {
            Ok(achievable_fps)
        }
    }
}

fn make_bitmapinfo(width: i32, height: i32) -> BITMAPINFO {
    let mut bmi = BITMAPINFO::default();
    bmi.bmiHeader.biSize = std::mem::size_of::<BITMAPINFOHEADER>() as u32;
    bmi.bmiHeader.biWidth = width;
    bmi.bmiHeader.biHeight = -height; // top-down DIB
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB.0 as u32;
    bmi.bmiHeader.biSizeImage = (width * height * 4) as u32;
    bmi
}

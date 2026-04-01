use std::collections::HashMap;

use pyo3::prelude::*;
use windows::Win32::Foundation::{HWND, LPARAM, RECT, TRUE};
use windows::Win32::Graphics::Gdi::{
    EnumDisplayMonitors, GetMonitorInfoW, HDC, HMONITOR, MONITORINFOEXW,
};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL,
    COINIT_MULTITHREADED, STGM_READ,
};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetClassNameW, GetWindowTextLengthW, GetWindowTextW,
    GetWindowThreadProcessId, IsWindow, IsWindowVisible,
};

// ── Monitor discovery ────────────────────────────────────────────

#[pyclass(frozen)]
#[derive(Clone)]
pub struct MonitorInfo {
    #[pyo3(get)]
    pub index: i32,
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub x: i32,
    #[pyo3(get)]
    pub y: i32,
    #[pyo3(get)]
    pub width: i32,
    #[pyo3(get)]
    pub height: i32,
    #[pyo3(get)]
    pub is_primary: bool,
}

#[pymethods]
impl MonitorInfo {
    fn as_dict(&self, py: Python<'_>) -> HashMap<String, PyObject> {
        let mut map = HashMap::new();
        map.insert("index".into(), self.index.into_pyobject(py).unwrap().into());
        map.insert("name".into(), self.name.clone().into_pyobject(py).unwrap().into());
        map.insert("x".into(), self.x.into_pyobject(py).unwrap().into());
        map.insert("y".into(), self.y.into_pyobject(py).unwrap().into());
        map.insert("width".into(), self.width.into_pyobject(py).unwrap().into());
        map.insert("height".into(), self.height.into_pyobject(py).unwrap().into());
        map.insert(
            "is_primary".into(),
            self.is_primary.into_pyobject(py).unwrap().to_owned().into(),
        );
        map
    }

    fn __repr__(&self) -> String {
        format!(
            "MonitorInfo(index={}, name='{}', {}x{} @ ({},{}){})",
            self.index,
            self.name,
            self.width,
            self.height,
            self.x,
            self.y,
            if self.is_primary { ", primary" } else { "" }
        )
    }
}

#[pyfunction]
pub fn list_monitors() -> PyResult<Vec<MonitorInfo>> {
    let mut monitors: Vec<MonitorInfo> = Vec::new();

    unsafe extern "system" fn callback(
        hmon: HMONITOR,
        _hdc: HDC,
        _lprect: *mut RECT,
        lparam: LPARAM,
    ) -> windows_core::BOOL {
        let monitors = &mut *(lparam.0 as *mut Vec<MonitorInfo>);
        let mut info = MONITORINFOEXW::default();
        info.monitorInfo.cbSize = std::mem::size_of::<MONITORINFOEXW>() as u32;
        let _ = GetMonitorInfoW(hmon, &mut info as *mut MONITORINFOEXW as *mut _);
        let rc = info.monitorInfo.rcMonitor;
        let name_slice = &info.szDevice;
        let name = String::from_utf16_lossy(name_slice)
            .trim_end_matches('\0')
            .to_string();
        monitors.push(MonitorInfo {
            index: monitors.len() as i32,
            name,
            x: rc.left,
            y: rc.top,
            width: rc.right - rc.left,
            height: rc.bottom - rc.top,
            is_primary: (info.monitorInfo.dwFlags & 1) != 0,
        });
        TRUE
    }

    unsafe {
        let lparam = LPARAM(&mut monitors as *mut Vec<MonitorInfo> as isize);
        let _ = EnumDisplayMonitors(None, None, Some(callback), lparam);
    }

    Ok(monitors)
}

// ── Window discovery ─────────────────────────────────────────────

#[pyclass(frozen)]
#[derive(Clone)]
pub struct WindowInfo {
    #[pyo3(get)]
    pub handle: isize,
    #[pyo3(get)]
    pub title: String,
    #[pyo3(get)]
    pub class_name: String,
    #[pyo3(get)]
    pub pid: u32,
    #[pyo3(get)]
    pub visible: bool,
}

#[pymethods]
impl WindowInfo {
    fn as_dict(&self, py: Python<'_>) -> HashMap<String, PyObject> {
        let mut map = HashMap::new();
        map.insert("handle".into(), self.handle.into_pyobject(py).unwrap().into());
        map.insert("title".into(), self.title.clone().into_pyobject(py).unwrap().into());
        map.insert(
            "class_name".into(),
            self.class_name.clone().into_pyobject(py).unwrap().into(),
        );
        map.insert("pid".into(), self.pid.into_pyobject(py).unwrap().into());
        map.insert(
            "visible".into(),
            self.visible.into_pyobject(py).unwrap().to_owned().into(),
        );
        map
    }

    fn __repr__(&self) -> String {
        format!(
            "WindowInfo(handle={:#x}, title='{}', pid={})",
            self.handle, self.title, self.pid
        )
    }
}

#[pyfunction]
#[pyo3(signature = (*, include_hidden=false))]
pub fn list_windows(include_hidden: bool) -> PyResult<Vec<WindowInfo>> {
    struct EnumCtx {
        windows: Vec<WindowInfo>,
        include_hidden: bool,
    }

    let mut ctx = EnumCtx {
        windows: Vec::new(),
        include_hidden,
    };

    unsafe extern "system" fn callback(hwnd: HWND, lparam: LPARAM) -> windows_core::BOOL {
        let ctx = &mut *(lparam.0 as *mut EnumCtx);
        let length = GetWindowTextLengthW(hwnd);
        if length == 0 && !ctx.include_hidden {
            return TRUE;
        }

        let visible = IsWindowVisible(hwnd).as_bool();
        if !visible && !ctx.include_hidden {
            return TRUE;
        }

        let mut title_buf = vec![0u16; (length + 1) as usize];
        GetWindowTextW(hwnd, &mut title_buf);
        let title = String::from_utf16_lossy(&title_buf)
            .trim_end_matches('\0')
            .to_string();

        let mut cls_buf = [0u16; 256];
        GetClassNameW(hwnd, &mut cls_buf);
        let class_name = String::from_utf16_lossy(&cls_buf)
            .trim_end_matches('\0')
            .to_string();

        let mut pid: u32 = 0;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));

        ctx.windows.push(WindowInfo {
            handle: hwnd.0 as isize,
            title,
            class_name,
            pid,
            visible,
        });
        TRUE
    }

    unsafe {
        let lparam = LPARAM(&mut ctx as *mut EnumCtx as isize);
        let _ = EnumWindows(Some(callback), lparam);
    }

    Ok(ctx.windows)
}

#[pyfunction]
pub fn find_window_by_title(substring: &str) -> PyResult<Option<WindowInfo>> {
    let lower = substring.to_lowercase();
    let windows = list_windows(false)?;
    Ok(windows
        .into_iter()
        .find(|w| w.title.to_lowercase().contains(&lower)))
}

#[pyfunction]
pub fn find_window_by_handle(hwnd: isize) -> PyResult<Option<WindowInfo>> {
    let hwnd_win = HWND(hwnd as *mut _);
    unsafe {
        if !IsWindow(Some(hwnd_win)).as_bool() {
            return Ok(None);
        }
        let length = GetWindowTextLengthW(hwnd_win);
        let mut title_buf = vec![0u16; (length + 1) as usize];
        GetWindowTextW(hwnd_win, &mut title_buf);
        let title = String::from_utf16_lossy(&title_buf)
            .trim_end_matches('\0')
            .to_string();

        let mut cls_buf = [0u16; 256];
        GetClassNameW(hwnd_win, &mut cls_buf);
        let class_name = String::from_utf16_lossy(&cls_buf)
            .trim_end_matches('\0')
            .to_string();

        let mut pid: u32 = 0;
        GetWindowThreadProcessId(hwnd_win, Some(&mut pid));
        let visible = IsWindowVisible(hwnd_win).as_bool();

        Ok(Some(WindowInfo {
            handle: hwnd,
            title,
            class_name,
            pid,
            visible,
        }))
    }
}

// ── Audio device discovery ───────────────────────────────────────

#[pyclass(frozen)]
#[derive(Clone)]
pub struct AudioDeviceInfo {
    #[pyo3(get)]
    pub id: String,
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub is_default: bool,
}

#[pymethods]
impl AudioDeviceInfo {
    fn as_dict(&self, py: Python<'_>) -> HashMap<String, PyObject> {
        let mut map = HashMap::new();
        map.insert("id".into(), self.id.clone().into_pyobject(py).unwrap().into());
        map.insert("name".into(), self.name.clone().into_pyobject(py).unwrap().into());
        map.insert(
            "is_default".into(),
            self.is_default.into_pyobject(py).unwrap().to_owned().into(),
        );
        map
    }

    fn __repr__(&self) -> String {
        format!(
            "AudioDeviceInfo(name='{}', is_default={})",
            self.name, self.is_default
        )
    }
}

#[pyfunction]
pub fn list_audio_devices() -> PyResult<Vec<AudioDeviceInfo>> {
    match enumerate_wasapi_devices() {
        Ok(devices) => Ok(devices),
        Err(_) => Ok(vec![AudioDeviceInfo {
            id: "default".to_string(),
            name: "Default Audio Device".to_string(),
            is_default: true,
        }]),
    }
}

fn enumerate_wasapi_devices() -> Result<Vec<AudioDeviceInfo>, Box<dyn std::error::Error>> {
    use windows::Win32::Media::Audio::{
        eConsole, eRender, IMMDevice, IMMDeviceCollection, IMMDeviceEnumerator,
        MMDeviceEnumerator, DEVICE_STATE_ACTIVE,
    };
    use windows::Win32::System::Com::StructuredStorage::PropVariantClear;
    use windows::Win32::Devices::FunctionDiscovery::PKEY_Device_FriendlyName;
    use windows::Win32::UI::Shell::PropertiesSystem::IPropertyStore;

    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);

        let enumerator: IMMDeviceEnumerator =
            CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)?;

        let default_device: IMMDevice =
            enumerator.GetDefaultAudioEndpoint(eRender, eConsole)?;
        let default_id = default_device.GetId()?.to_string().unwrap_or_default();

        let collection: IMMDeviceCollection =
            enumerator.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)?;
        let count = collection.GetCount()?;

        let mut devices = Vec::new();
        for i in 0..count {
            let device: IMMDevice = collection.Item(i)?;
            let dev_id = device.GetId()?.to_string().unwrap_or_default();

            let mut name = format!("Audio Device {}", i);
            if let Ok(store) = device.OpenPropertyStore(STGM_READ) {
                let store: IPropertyStore = store;
                if let Ok(pv) = store.GetValue(&PKEY_Device_FriendlyName as *const _) {
                    // PROPVARIANT for VT_LPWSTR: the pwszVal field
                    let vt = pv.Anonymous.Anonymous.vt;
                    if vt.0 == 31 {
                        // VT_LPWSTR = 31
                        let pwsz = pv.Anonymous.Anonymous.Anonymous.pwszVal;
                        if !pwsz.is_null() {
                            if let Ok(s) = pwsz.to_string() {
                                name = s;
                            }
                        }
                    }
                    let mut pv_mut = pv;
                    let _ = PropVariantClear(&mut pv_mut);
                }
            }

            devices.push(AudioDeviceInfo {
                id: dev_id.clone(),
                name,
                is_default: dev_id == default_id,
            });
        }

        CoUninitialize();
        Ok(devices)
    }
}

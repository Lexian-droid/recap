mod audio;
mod discovery;
mod video;

use pyo3::prelude::*;

#[pymodule]
fn _rust_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Discovery
    m.add_class::<discovery::MonitorInfo>()?;
    m.add_class::<discovery::WindowInfo>()?;
    m.add_class::<discovery::AudioDeviceInfo>()?;
    m.add_function(wrap_pyfunction!(discovery::list_monitors, m)?)?;
    m.add_function(wrap_pyfunction!(discovery::list_windows, m)?)?;
    m.add_function(wrap_pyfunction!(discovery::list_audio_devices, m)?)?;
    m.add_function(wrap_pyfunction!(discovery::find_window_by_title, m)?)?;
    m.add_function(wrap_pyfunction!(discovery::find_window_by_handle, m)?)?;

    // Video
    m.add_class::<video::VideoCapture>()?;

    // Audio
    m.add_class::<audio::AudioCapture>()?;

    Ok(())
}

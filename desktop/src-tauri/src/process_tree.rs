//! Windows process-tree ownership for the Desktop Runtime.

#[cfg(windows)]
mod platform {
    use std::{ffi::c_void, mem::size_of, ptr::null};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GetLastError, HANDLE},
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::GetCurrentProcess,
        },
    };

    /// Keeps the Shell and every inherited Engine worker in one kill-on-close Job.
    pub struct ProcessTreeJob {
        handle: HANDLE,
    }

    impl ProcessTreeJob {
        pub fn create() -> Result<Self, String> {
            unsafe {
                let handle = CreateJobObjectW(null(), null());
                if handle.is_null() {
                    return Err(last_error(
                        "Could not create the Desktop Runtime Job Object",
                    ));
                }

                let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION as *const c_void,
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                ) == 0
                {
                    let error = last_error("Could not configure the Desktop Runtime Job Object");
                    CloseHandle(handle);
                    return Err(error);
                }

                if AssignProcessToJobObject(handle, GetCurrentProcess()) == 0 {
                    let error =
                        last_error("Could not assign OpenKB to the Desktop Runtime Job Object");
                    CloseHandle(handle);
                    return Err(error);
                }

                Ok(Self { handle })
            }
        }
    }

    impl Drop for ProcessTreeJob {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }

    fn last_error(prefix: &str) -> String {
        unsafe { format!("{prefix} (Windows error {}).", GetLastError()) }
    }
}

#[cfg(not(windows))]
mod platform {
    /// Allows source checks outside Windows; packaging enforces Job Objects on Windows.
    pub struct ProcessTreeJob;

    impl ProcessTreeJob {
        pub fn create() -> Result<Self, String> {
            Ok(Self)
        }
    }
}

pub use platform::ProcessTreeJob;

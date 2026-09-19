#[cfg(not(target_os = "linux"))]
compile_error!(
    "Build the Engine inside Linux. See docs/00-environment.md for a Linux VM and RXE setup."
);

mod bindings;
mod engine;
mod ffi;

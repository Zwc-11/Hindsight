fn main() {
    if let Ok(rustc) = std::env::var("RUSTC") {
        if let Ok(output) = std::process::Command::new(rustc).arg("--version").output() {
            if output.status.success() {
                println!(
                    "cargo:rustc-env=HINDSIGHT_RUSTC_VERSION={}",
                    String::from_utf8_lossy(&output.stdout).trim()
                );
            }
        }
    }
    cxx_build::bridge("src/numerics.rs")
        .file("../../cpp/numerics/ridge.cpp")
        .include("../../cpp/numerics")
        .std("c++20")
        .warnings(true)
        .compile("hindsight-ridge");
    for path in [
        "src/numerics.rs",
        "../../cpp/numerics/ridge.cpp",
        "../../cpp/numerics/ridge.hpp",
    ] {
        println!("cargo:rerun-if-changed={path}");
    }
}

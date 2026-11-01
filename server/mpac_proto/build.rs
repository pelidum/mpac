use std::env;
use std::error::Error;
use std::path::{Path, PathBuf};

const WKT_SUFFIX: &str = "google/protobuf/timestamp.proto";

fn well_known_include_dir() -> Result<PathBuf, Box<dyn Error>> {
    let paths = env::var("PROTOBUF_WELL_KNOWN_PROTOS")?;
    let timestamp = paths
        .split_whitespace()
        .find(|p| p.ends_with(WKT_SUFFIX))
        .ok_or("timestamp.proto missing from PROTOBUF_WELL_KNOWN_PROTOS")?;
    Ok(PathBuf::from(
        &timestamp[..timestamp.len() - WKT_SUFFIX.len()],
    ))
}

fn main() -> Result<(), Box<dyn Error>> {
    let proto_root = PathBuf::from(env::var("MPAC_PROTO_ROOT")?);
    let out_dir = PathBuf::from(env::var("OUT_DIR")?);
    let protos = [
        proto_root.join(Path::new("proto/service.proto")),
        proto_root.join(Path::new("proto/jobs.proto")),
    ];

    let mut config = prost_build::Config::new();
    config
        .enable_type_names()
        .file_descriptor_set_path(out_dir.join("mpac_descriptor.bin"));

    tonic_prost_build::configure().compile_with_config(
        config,
        &protos,
        &[proto_root.clone(), well_known_include_dir()?],
    )?;
    Ok(())
}

// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// Bring up both protocol servers against a virtual instrument, put a
// fault-injecting proxy in front of each, and open the control socket.
//
// Ports default to 0 (ephemeral) so that many instances can run at once —
// checks run in parallel, and a suite that needs a fixed port is a suite that
// cannot. The chosen ports are printed as one JSON line on stdout before
// anything starts serving, which is what the harness reads to find the
// control socket. That line is the startup handshake: a harness that polls a
// guessed port instead races the bind and fails about one run in fifty.

use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Parser;
use tokio::net::TcpListener;
use tokio::sync::Mutex;

use testgear_mock_server::backend::GpibBackend;
use testgear_mock_server::control::{self, Control, PortInfo};
use testgear_mock_server::faults::Faults;
use testgear_mock_server::frontend::instrument::Instrument;
use testgear_mock_server::frontend::lock::LockRegistry;
use testgear_mock_server::observe::Observed;
use testgear_mock_server::proxy;
use testgear_mock_server::virtual_instrument::VirtualInstrument;
use testgear_mock_server::{hislip, vxi11};

#[derive(Parser, Debug)]
#[command(
    name = "testgear-mock-server",
    about = "HiSLIP and VXI-11 servers backed by a virtual instrument, with fault injection"
)]
struct Args {
    /// Address to serve on.
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    /// VXI-11 core channel port. 0 picks an ephemeral one.
    #[arg(long, default_value_t = 0)]
    vxi11_port: u16,

    /// HiSLIP port. 0 picks an ephemeral one.
    #[arg(long, default_value_t = 0)]
    hislip_port: u16,

    /// Control channel port. 0 picks an ephemeral one.
    #[arg(long, default_value_t = 0)]
    control_port: u16,

    /// Primary addresses to put a simulated instrument at.
    #[arg(long, value_delimiter = ',', default_values_t = [0u8, 14u8, 23u8])]
    pads: Vec<u8>,

    /// Serve the protocols directly, with no fault-injecting proxy in front.
    /// Transport faults stop working; useful when measuring throughput, where
    /// the proxy's extra copy is the thing being measured.
    #[arg(long)]
    no_proxy: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "warn".into()),
        )
        .with_writer(std::io::stderr)
        .init();

    let args = Args::parse();
    let faults = Arc::new(Faults::new());
    let observed = Arc::new(Observed::new());
    let instrument = VirtualInstrument::shared(&args.pads, faults.clone(), observed.clone());
    // One registry for both front-ends: a lock taken over HiSLIP has to
    // exclude I/O arriving over VXI-11, or the cross-protocol lock checks
    // would pass against a server that does not actually interlock them.
    let locks = Arc::new(LockRegistry::new());

    // The servers themselves always bind loopback ephemeral ports; what the
    // client connects to is the proxy. With --no-proxy the servers take the
    // requested ports directly.
    let (vxi11_listener, vxi11_public, vxi11_proxy) =
        bind_pair(&args.host, args.vxi11_port, args.no_proxy).await?;
    let (hislip_listener, hislip_public, hislip_proxy) =
        bind_pair(&args.host, args.hislip_port, args.no_proxy).await?;
    let control_listener = TcpListener::bind((args.host.as_str(), args.control_port))
        .await
        .context("binding the control port")?;
    let control_port = control_listener.local_addr()?.port();

    let ports = PortInfo {
        vxi11_port: vxi11_public,
        hislip_port: hislip_public,
        control_port,
        // pyvisa accepts "host,port" in the board field, which is what makes
        // a VXI-11 server on an ephemeral port reachable at all: the normal
        // path asks the portmapper on 111, and a test that needed to bind a
        // privileged port could not run unprivileged or in parallel.
        vxi11_resource: format!("TCPIP0::{},{}::inst0::INSTR", args.host, vxi11_public),
        hislip_resource: format!("TCPIP0::{}::hislip0,{}::INSTR", args.host, hislip_public),
    };

    // Announce before serving: the harness blocks on this line, so printing
    // it after the accept loops start would be a race it could lose.
    println!("{}", serde_json::to_string(&ports)?);
    use std::io::Write;
    std::io::stdout().flush()?;

    let control = Arc::new(Control {
        ports,
        faults: faults.clone(),
        observed: observed.clone(),
        instrument: instrument.clone(),
    });

    let backend: Arc<Mutex<dyn GpibBackend>> = instrument.clone();
    let default_pad = *args.pads.first().unwrap_or(&0);

    let vxi11_ctrl = backend.clone();
    let vxi11_locks = locks.clone();
    let vxi11_fut = async move {
        let config = vxi11::server::Config {
            default_pad,
            locks: vxi11_locks,
            ..Default::default()
        };
        let instrument_for = move |pad: u8| Arc::new(Instrument::new(vxi11_ctrl.clone(), pad));
        vxi11::server::run(vxi11_listener, config, instrument_for)
            .await
            .map_err(anyhow::Error::from)
    };

    let hislip_ctrl = backend.clone();
    let hislip_locks = locks.clone();
    let hislip_fut = async move {
        let config = hislip::server::Config {
            locks: hislip_locks,
            ..Default::default()
        };
        let device_for = move |subaddr: &str| {
            let pad = hislip::server::parse_subaddress_pad(subaddr).unwrap_or(default_pad);
            let dev: Arc<dyn hislip::server::Device> = Arc::new(
                hislip::instrument::GpibInstrument::new(hislip_ctrl.clone(), pad),
            );
            Some(dev)
        };
        hislip::server::run(hislip_listener, config, device_for)
            .await
            .map_err(anyhow::Error::from)
    };

    let vxi11_proxy_fut = optional_proxy(vxi11_proxy, faults.clone());
    let hislip_proxy_fut = optional_proxy(hislip_proxy, faults.clone());
    let control_fut = control::run(control_listener, control);

    // Any one of these ending means the server is no longer serving what it
    // advertised. Failing the whole process is right: a half-dead mock
    // produces check failures that look like findings.
    tokio::try_join!(
        vxi11_fut,
        hislip_fut,
        vxi11_proxy_fut,
        hislip_proxy_fut,
        control_fut
    )?;
    Ok(())
}

/// Bind the port the client will use and, unless proxying is off, a private
/// loopback port for the server behind it.
///
/// Returns `(server listener, public port, proxy listener)`.
async fn bind_pair(
    host: &str,
    port: u16,
    no_proxy: bool,
) -> Result<(TcpListener, u16, Option<(TcpListener, String)>)> {
    let public = TcpListener::bind((host, port))
        .await
        .with_context(|| format!("binding {host}:{port}"))?;
    let public_port = public.local_addr()?.port();

    if no_proxy {
        return Ok((public, public_port, None));
    }

    let private = TcpListener::bind((host, 0))
        .await
        .context("binding the private server port")?;
    let upstream = private.local_addr()?.to_string();
    Ok((private, public_port, Some((public, upstream))))
}

async fn optional_proxy(
    listener: Option<(TcpListener, String)>,
    faults: Arc<Faults>,
) -> Result<()> {
    match listener {
        Some((listener, upstream)) => proxy::run(listener, upstream, faults).await,
        None => std::future::pending::<Result<()>>().await,
    }
}

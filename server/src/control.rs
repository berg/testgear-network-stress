// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// The control channel: newline-delimited JSON over TCP, one request per line,
// one response per line.
//
// Deliberately not part of either instrument protocol. A check that wanted to
// arm a fault by sending a magic SCPI command would be injecting its own
// traffic into the very stream it is making assertions about, and the arming
// message would show up in the observation log it then reads back. A separate
// socket keeps the thing being measured and the thing doing the measuring on
// different wires.
//
// The protocol is line-JSON rather than anything richer because the client is
// a test harness in another language that must not need a dependency to talk
// to it: `socket.sendall(json.dumps(...) + "\n")` is the entire client.

use std::sync::Arc;

use anyhow::Result;
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Mutex;
use tracing::debug;

use crate::faults::{FaultConfig, Faults};
use crate::observe::{Event, Observed};
use crate::virtual_instrument::{Device, VirtualInstrument};
use crate::vxi11_fault::{Tracker, Vxi11Faults};

/// What the harness can ask for.
#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    /// Report the listening ports and the resource strings that reach them.
    Ports,
    /// Set some subset of the fault knobs.
    Faults { config: FaultConfig },
    /// Put every knob back to normal and empty the observation log. What a
    /// check calls between cases.
    Reset,
    /// The instrument-side event log.
    Observed,
    /// Empty the observation log without touching the fault knobs.
    ClearObserved,
    /// Set a scripted answer for a query, or drop one with `response: null`.
    Respond {
        pad: u8,
        query: String,
        response: Option<String>,
    },
    /// Size of the reply to `TEST:BIG?`, for the chunking checks.
    BigReply { pad: u8, bytes: usize },
    /// Push a status-byte bit set, so a check can raise SRQ on demand.
    SetStb { pad: u8, bits: u8 },
    /// Arm the RPC-level VXI-11 faults.
    Vxi11Faults { config: Vxi11Faults },
    /// Ping, so the harness can wait for the server to come up.
    Ping,
}

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum Response {
    Ports(PortInfo),
    Faults(FaultConfig),
    Events { events: Vec<Event> },
    Ok { ok: bool },
    Error { error: String },
}

#[derive(Clone, Debug, Serialize)]
pub struct PortInfo {
    pub vxi11_port: u16,
    pub hislip_port: u16,
    pub control_port: u16,
    /// Resource strings that reach this server, ready to hand to pyvisa.
    pub vxi11_resource: String,
    pub hislip_resource: String,
}

pub struct Control {
    pub ports: PortInfo,
    pub faults: Arc<Faults>,
    pub observed: Arc<Observed>,
    pub instrument: Arc<Mutex<VirtualInstrument>>,
    pub vxi11: Arc<Tracker>,
}

pub async fn run(listener: TcpListener, ctl: Arc<Control>) -> Result<()> {
    loop {
        let (stream, _) = listener.accept().await?;
        let ctl = ctl.clone();
        tokio::spawn(async move {
            if let Err(err) = serve(stream, ctl).await {
                debug!(%err, "control connection ended");
            }
        });
    }
}

async fn serve(stream: TcpStream, ctl: Arc<Control>) -> Result<()> {
    let (rx, mut tx) = stream.into_split();
    let mut lines = BufReader::new(rx).lines();
    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        let response = match serde_json::from_str::<Request>(&line) {
            Ok(request) => handle(request, &ctl).await,
            // A malformed request is answered, not dropped: a harness waiting
            // on a reply that never comes reports a hang, and the actual
            // problem — a typo in a field name — is then invisible.
            Err(err) => Response::Error {
                error: format!("bad request: {err}"),
            },
        };
        let mut body = serde_json::to_vec(&response)?;
        body.push(b'\n');
        tx.write_all(&body).await?;
        tx.flush().await?;
    }
    Ok(())
}

async fn handle(request: Request, ctl: &Control) -> Response {
    match request {
        Request::Ping => Response::Ok { ok: true },
        Request::Ports => Response::Ports(ctl.ports.clone()),
        Request::Faults { config } => {
            ctl.faults.apply(config);
            Response::Faults(ctl.faults.snapshot())
        }
        Request::Reset => {
            ctl.faults.reset();
            ctl.vxi11.clear();
            ctl.observed.clear();
            Response::Ok { ok: true }
        }
        Request::Vxi11Faults { config } => {
            ctl.vxi11.set(config);
            Response::Ok { ok: true }
        }
        Request::Observed => Response::Events {
            events: ctl.observed.snapshot(),
        },
        Request::ClearObserved => {
            ctl.observed.clear();
            Response::Ok { ok: true }
        }
        Request::Respond {
            pad,
            query,
            response,
        } => with_device(ctl, pad, |dev| dev.set_response(&query, response)).await,
        Request::BigReply { pad, bytes } => {
            with_device(ctl, pad, |dev| dev.set_big_reply_len(bytes)).await
        }
        Request::SetStb { pad, bits } => {
            // Raising the bits is not enough on its own. HiSLIP does not
            // serial-poll on every status query -- it synthesises the status
            // byte server-side and relies on the SRQ forwarder for the rest
            // -- so a status set without the resulting service request is a
            // status no HiSLIP client will ever observe.
            let mut guard = ctl.instrument.lock().await;
            match guard.device_mut(pad) {
                Ok(dev) => {
                    dev.set_user_stb(bits);
                    guard.update_srq(pad);
                    Response::Ok { ok: true }
                }
                Err(err) => Response::Error {
                    error: err.to_string(),
                },
            }
        }
    }
}

/// Run `f` against one simulated device, reporting a missing address as an
/// error rather than a panic — a check that names the wrong PAD should be
/// told so, not crash the server every other check is sharing.
async fn with_device<F>(ctl: &Control, pad: u8, f: F) -> Response
where
    F: FnOnce(&mut Device),
{
    let mut guard = ctl.instrument.lock().await;
    match guard.device_mut(pad) {
        Ok(dev) => {
            f(dev);
            Response::Ok { ok: true }
        }
        Err(err) => Response::Error {
            error: err.to_string(),
        },
    }
}

// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// The knobs. Every field here exists to produce one condition that a bench
// produces about once a year and never on demand.
//
// They divide into two kinds, and the division is deliberate:
//
// - *Instrument* faults (`before_read`, `forced_stb`, ...) are things a device
//   at the end of the bus does: answer late, answer nothing, report a status
//   byte that does not match its queue.
//
// - *Transport* faults (`drop_after_bytes`, `stall_after_bytes`, ...) are
//   things the network does: a connection that goes away mid-message, a reply
//   split across segments at an inconvenient boundary, bytes that stop
//   arriving with the client still waiting.
//
// Transport faults are applied by the proxy in `proxy.rs`, in front of an
// unmodified server, rather than by hooks inside it. That is what keeps the
// vendored HiSLIP and VXI-11 code re-vendorable from ugpibd without a merge:
// no test scaffolding was ever added to it. It also means the faults are
// applied where the client actually experiences them — at the socket — so a
// client bug cannot be masked by an obliging server noticing the injection.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::time::Duration;

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

/// A `None`-able u32 in an atomic: `u64::MAX` is the sentinel for unset.
const UNSET: u64 = u64::MAX;

#[derive(Default)]
pub struct Faults {
    // -- instrument-side ---------------------------------------------------
    /// Milliseconds the instrument sleeps before answering a read.
    read_delay_ms: AtomicU32,
    /// Milliseconds the instrument sleeps before accepting a write.
    write_delay_ms: AtomicU32,
    /// Fail the next write outright, as a bus error would.
    fail_next_write: AtomicBool,
    /// Report this status byte from every serial poll, whatever the queue says.
    forced_stb: AtomicU64,

    // -- transport-side, applied by the proxy ------------------------------
    /// Close the TCP connection abruptly after this many bytes have been sent
    /// to the client. Models an instrument that resets mid-reply.
    drop_after_bytes: AtomicU64,
    /// Stop forwarding after this many bytes without closing: the client is
    /// left waiting on a connection that is still open and never answers,
    /// which is the condition a read timeout is supposed to end and a
    /// deadline bug turns into a hang.
    stall_after_bytes: AtomicU64,
    /// Forward server->client data one byte per write, with a small delay.
    /// A reply arriving in many segments is legal and a client that assumes
    /// one recv() per message breaks on it.
    dribble: AtomicBool,
    /// Milliseconds of delay inserted on every forwarded chunk.
    latency_ms: AtomicU32,
}

/// The wire form of the knobs, as the control channel sets them. Every field
/// is optional: a control message carries only what it changes.
#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FaultConfig {
    pub read_delay_ms: Option<u32>,
    pub write_delay_ms: Option<u32>,
    pub fail_next_write: Option<bool>,
    pub forced_stb: Option<Option<u8>>,
    pub drop_after_bytes: Option<Option<u64>>,
    pub stall_after_bytes: Option<Option<u64>>,
    pub dribble: Option<bool>,
    pub latency_ms: Option<u32>,
}

impl Faults {
    pub fn new() -> Self {
        let f = Self::default();
        f.forced_stb.store(UNSET, Ordering::Relaxed);
        f.drop_after_bytes.store(UNSET, Ordering::Relaxed);
        f.stall_after_bytes.store(UNSET, Ordering::Relaxed);
        f
    }

    /// Put every knob back to "behave normally". Checks call this between
    /// cases so a fault left set cannot leak into the next one and be
    /// mistaken for a finding.
    pub fn reset(&self) {
        self.read_delay_ms.store(0, Ordering::Relaxed);
        self.write_delay_ms.store(0, Ordering::Relaxed);
        self.fail_next_write.store(false, Ordering::Relaxed);
        self.forced_stb.store(UNSET, Ordering::Relaxed);
        self.drop_after_bytes.store(UNSET, Ordering::Relaxed);
        self.stall_after_bytes.store(UNSET, Ordering::Relaxed);
        self.dribble.store(false, Ordering::Relaxed);
        self.latency_ms.store(0, Ordering::Relaxed);
    }

    pub fn apply(&self, config: FaultConfig) {
        if let Some(v) = config.read_delay_ms {
            self.read_delay_ms.store(v, Ordering::Relaxed);
        }
        if let Some(v) = config.write_delay_ms {
            self.write_delay_ms.store(v, Ordering::Relaxed);
        }
        if let Some(v) = config.fail_next_write {
            self.fail_next_write.store(v, Ordering::Relaxed);
        }
        if let Some(v) = config.forced_stb {
            self.forced_stb
                .store(v.map_or(UNSET, u64::from), Ordering::Relaxed);
        }
        if let Some(v) = config.drop_after_bytes {
            self.drop_after_bytes.store(v.unwrap_or(UNSET), Ordering::Relaxed);
        }
        if let Some(v) = config.stall_after_bytes {
            self.stall_after_bytes
                .store(v.unwrap_or(UNSET), Ordering::Relaxed);
        }
        if let Some(v) = config.dribble {
            self.dribble.store(v, Ordering::Relaxed);
        }
        if let Some(v) = config.latency_ms {
            self.latency_ms.store(v, Ordering::Relaxed);
        }
    }

    pub fn snapshot(&self) -> FaultConfig {
        let opt = |a: &AtomicU64| match a.load(Ordering::Relaxed) {
            UNSET => None,
            v => Some(v),
        };
        FaultConfig {
            read_delay_ms: Some(self.read_delay_ms.load(Ordering::Relaxed)),
            write_delay_ms: Some(self.write_delay_ms.load(Ordering::Relaxed)),
            fail_next_write: Some(self.fail_next_write.load(Ordering::Relaxed)),
            forced_stb: Some(opt(&self.forced_stb).map(|v| v as u8)),
            drop_after_bytes: Some(opt(&self.drop_after_bytes)),
            stall_after_bytes: Some(opt(&self.stall_after_bytes)),
            dribble: Some(self.dribble.load(Ordering::Relaxed)),
            latency_ms: Some(self.latency_ms.load(Ordering::Relaxed)),
        }
    }

    // -- instrument hooks --------------------------------------------------
    pub async fn before_read(&self) -> Result<()> {
        let delay = self.read_delay_ms.load(Ordering::Relaxed);
        if delay > 0 {
            tokio::time::sleep(Duration::from_millis(delay as u64)).await;
        }
        Ok(())
    }

    pub async fn before_write(&self, _data: &[u8]) -> Result<()> {
        let delay = self.write_delay_ms.load(Ordering::Relaxed);
        if delay > 0 {
            tokio::time::sleep(Duration::from_millis(delay as u64)).await;
        }
        if self.fail_next_write.swap(false, Ordering::Relaxed) {
            bail!("injected bus write failure");
        }
        Ok(())
    }

    pub fn forced_stb(&self) -> Option<u8> {
        match self.forced_stb.load(Ordering::Relaxed) {
            UNSET => None,
            v => Some(v as u8),
        }
    }

    // -- transport hooks, read by the proxy --------------------------------
    pub fn drop_after(&self) -> Option<u64> {
        match self.drop_after_bytes.load(Ordering::Relaxed) {
            UNSET => None,
            v => Some(v),
        }
    }

    pub fn stall_after(&self) -> Option<u64> {
        match self.stall_after_bytes.load(Ordering::Relaxed) {
            UNSET => None,
            v => Some(v),
        }
    }

    pub fn dribble(&self) -> bool {
        self.dribble.load(Ordering::Relaxed)
    }

    pub fn latency(&self) -> Option<Duration> {
        match self.latency_ms.load(Ordering::Relaxed) {
            0 => None,
            v => Some(Duration::from_millis(v as u64)),
        }
    }
}

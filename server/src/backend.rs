// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 ugpibd contributors
// Copyright (C) 2026 testgear-network-stress contributors
//
// The pluggable GPIB adapter abstraction, vendored from ugpibd (src/backend/mod.rs)
// with the USB adapter machinery removed. Only the trait survives: this crate
// serves protocol front-ends backed by a virtual instrument, never by hardware,
// but the front-ends are ugpibd's own and speak to instruments through this.
//
// Keeping the trait byte-identical to ugpibd's matters. It is what lets the
// vendored HiSLIP and VXI-11 servers be re-vendored from upstream without
// adaptation, so a divergence between this copy and ugpibd shows up as a
// compile error rather than as a behavioural difference nobody notices.

use std::sync::Arc;

use anyhow::Result;
use tokio::sync::Mutex;



/// The daemon shares one opened adapter across both front-ends behind this.
pub type SharedBackend = Arc<Mutex<dyn GpibBackend>>;

/// A live read of the eight GPIB control lines.
///
/// Level, not edge: this answers "what is the bus doing *right now*". That is
/// what separates diagnoses which otherwise look identical from the data path
/// — an instrument that is silent from one that is talking to somebody else,
/// or a read that returns nothing because nothing was sent from one that
/// returns nothing because another controller holds ATN.
///
/// Both supported chips expose these in a single register with the same bit
/// layout (TMS9914 `BSR`, TNT4882 `BSR`), so the decode is shared.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct BusLines {
    pub ren: bool,
    pub ifc: bool,
    pub srq: bool,
    pub eoi: bool,
    pub nrfd: bool,
    pub ndac: bool,
    pub dav: bool,
    pub atn: bool,
    /// The register byte this was decoded from, so callers can report what was
    /// actually read and not only our interpretation of it.
    pub raw: u8,
}

impl BusLines {
    pub const REN: u8 = 0x01;
    pub const IFC: u8 = 0x02;
    pub const SRQ: u8 = 0x04;
    pub const EOI: u8 = 0x08;
    pub const NRFD: u8 = 0x10;
    pub const NDAC: u8 = 0x20;
    pub const DAV: u8 = 0x40;
    pub const ATN: u8 = 0x80;

    pub fn from_bsr(raw: u8) -> Self {
        Self {
            ren: raw & Self::REN != 0,
            ifc: raw & Self::IFC != 0,
            srq: raw & Self::SRQ != 0,
            eoi: raw & Self::EOI != 0,
            nrfd: raw & Self::NRFD != 0,
            ndac: raw & Self::NDAC != 0,
            dav: raw & Self::DAV != 0,
            atn: raw & Self::ATN != 0,
            raw,
        }
    }
}

impl std::fmt::Display for BusLines {
    /// `0x29 REN EOI NDAC` — the raw byte first, then the asserted lines.
    ///
    /// The raw byte is not redundant: if the bit order is ever wrong on some
    /// adapter, a reading that names the wrong lines is indistinguishable from
    /// a strange bus unless the byte it came from is also on screen.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:#04x}", self.raw)?;
        for (on, name) in [
            (self.atn, "ATN"),
            (self.dav, "DAV"),
            (self.ndac, "NDAC"),
            (self.nrfd, "NRFD"),
            (self.eoi, "EOI"),
            (self.srq, "SRQ"),
            (self.ifc, "IFC"),
            (self.ren, "REN"),
        ] {
            if on {
                write!(f, " {name}")?;
            }
        }
        if self.raw == 0 {
            write!(f, " (none asserted)")?;
        }
        Ok(())
    }
}

/// A single GPIB controller adapter, addressing instruments on its bus by
/// primary address (`pad`). Methods take `&mut self`; the daemon shares one
/// instance across both front-ends behind an `Arc<Mutex<dyn GpibBackend>>`.
#[async_trait::async_trait]
pub trait GpibBackend: Send + Sync {
    /// Bring the controller up as system controller: reset, assert IFC, REN.
    /// `my_pad` is the controller's own primary address (conventionally 0).
    async fn init(&mut self, my_pad: u8) -> Result<()>;

    /// Address the instrument at `pad` as listener and write `data`, asserting
    /// EOI on the final byte when `send_eoi` is set.
    async fn write(&mut self, pad: u8, data: &[u8], send_eoi: bool) -> Result<()>;

    /// Address the instrument at `pad` as talker and read up to `max_len`
    /// bytes. Returns the data and whether the message ended (EOI/EOS seen).
    async fn read(&mut self, pad: u8, max_len: usize) -> Result<(Vec<u8>, bool)>;

    /// Selected Device Clear to the instrument at `pad`.
    async fn device_clear(&mut self, pad: u8) -> Result<()>;

    /// Group Execute Trigger to the instrument at `pad`.
    async fn trigger(&mut self, pad: u8) -> Result<()>;

    /// Pulse Interface Clear, returning the bus to idle.
    async fn ifc(&mut self) -> Result<()>;

    /// Assert or deassert Remote Enable.
    async fn ren(&mut self, enable: bool) -> Result<()>;

    /// Address the instrument at `pad` as listener, which is what puts it into
    /// remote state while REN is asserted.
    ///
    /// Addressing alone does it — REN gates the transition, the listen address
    /// triggers it — so this is `ren(true)` plus an addressing sequence, and is
    /// how `viGpibControlREN(VI_GPIB_REN_ASSERT_ADDRESS)` differs from a plain
    /// assert.
    async fn go_to_remote(&mut self, pad: u8) -> Result<()>;

    /// Send Go To Local (GTL) to the instrument at `pad`, returning that one
    /// device to front-panel control.
    ///
    /// Addressed, unlike dropping REN, which returns *every* device on the bus
    /// to local. Note the effect is undone by the next write to the device:
    /// addressing it as a listener with REN still asserted puts it straight
    /// back into remote, which is the standard's behaviour and not a bug here.
    async fn go_to_local(&mut self, pad: u8) -> Result<()>;

    /// Send Local Lockout (LLO), disabling the front-panel local key on every
    /// device on the bus.
    ///
    /// Universal, so it takes no address: the standard offers no per-device
    /// lockout. Cleared by dropping REN.
    async fn local_lockout(&mut self) -> Result<()>;

    /// Serial-poll the instrument at `pad` and return its status byte. This
    /// backs both the Prologix `++spoll` and the HiSLIP `get_status` operation.
    ///
    /// There is deliberately no default: a status byte of 0 means "no bits set,
    /// nothing to report", so a backend that silently returned one would be
    /// indistinguishable from a working serial poll and would hang any script
    /// that polls until a bit sets. A backend that cannot serial-poll must say
    /// so, exactly as `srq_asserted` does below.
    async fn serial_poll(&mut self, pad: u8) -> Result<u8>;

    /// Whether the SRQ line is currently asserted by some device on the bus.
    ///
    /// This is a level read of the physical line, not an event: it answers
    /// "is anyone requesting service right now". The default reports that the
    /// backend cannot tell, which callers must surface as an error rather than
    /// as "no SRQ" — a fabricated "no" is indistinguishable from a working bus
    /// and silently breaks any script that polls for service requests.
    async fn srq_asserted(&mut self) -> Result<bool> {
        anyhow::bail!("{} cannot read the SRQ line", self.name())
    }

    /// Read all eight GPIB control lines as they stand right now.
    ///
    /// A superset of `srq_asserted`, and the tool for telling apart failures
    /// that look identical from the data path: whether a read returned nothing
    /// because nothing was sent, or because another controller holds ATN and
    /// we are no longer in charge.
    ///
    /// The default refuses for the same reason `srq_asserted` does — a
    /// fabricated all-clear reading is indistinguishable from a real one, and
    /// would mislead exactly the person trying to diagnose a bus.
    async fn bus_lines(&mut self) -> Result<BusLines> {
        anyhow::bail!("{} cannot read the GPIB control lines", self.name())
    }

    /// Enter or leave unaddressed-listen ("listen only") mode.
    ///
    /// Two things change together, and both are required — the second is the
    /// one that is easy to miss:
    ///
    /// 1. The chip becomes an **unaddressed listener**, accepting every data
    ///    byte on the bus regardless of who is addressed. This is the only way
    ///    to receive from a talk-only source, which by construction has no
    ///    address to point a read at (`docs/CAPTURE.md` §14.2).
    /// 2. The **RFD holdoff is released**. Normal operation holds NRFD between
    ///    reads so bytes are not dropped, but that presents a listener which is
    ///    never ready, and a talk-only talker will refuse to transmit to it —
    ///    an HP 53310A reports "no ready listeners?" (§4.7). While capturing we
    ///    must be continuously ready, which means giving up that flow control.
    ///
    /// Because of (2) this is **mutually exclusive with ordinary controller
    /// traffic**: with the holdoff gone, bytes can arrive between reads with
    /// nowhere to go. Callers must refuse addressed operations while it is on
    /// rather than let them silently corrupt a capture.
    ///
    /// Runtime-switchable by design: both halves are register writes needing no
    /// re-initialisation, so this is a mode the daemon enters and leaves, not a
    /// mode it must be started in.
    async fn set_listen_only(&mut self, enable: bool) -> Result<()> {
        let _ = enable;
        anyhow::bail!("{} cannot enter listen-only mode", self.name())
    }

    /// Whether unaddressed-listen is currently on.
    fn listen_only(&self) -> bool {
        false
    }

    /// Become an addressable *device* at `address`, or return to controller.
    ///
    /// This is not listen-only. In listen-only we are still controller and
    /// simply accept every byte; here we stop being a controller at all —
    /// system control is released, REN and IFC are dropped, and we sit at a
    /// primary address waiting for somebody else to address us.
    ///
    /// It is what an instrument that drives its own plot transfer needs. An
    /// SR620 with a plotter address configured emits **nothing at all** until a
    /// device exists at that address: sampled continuously while PRINT was
    /// pressed, not one bus line moved (`docs/CAPTURE.md` §14.17). Listen-only
    /// cannot help there, because there is no traffic to listen to.
    ///
    /// Note what this costs: we are no longer controller-in-charge, so the
    /// "pulse IFC to recover the bus" escape hatch does not apply while it is
    /// on. Returning to controller mode re-initialises the adapter.
    async fn set_device_mode(&mut self, address: Option<u8>) -> Result<()> {
        let _ = address;
        anyhow::bail!("{} cannot act as a GPIB device", self.name())
    }

    /// The address we are answering to as a device, if any.
    fn device_address(&self) -> Option<u8> {
        None
    }

    /// Subscribe to service-request notifications, for adapters that can report
    /// SRQ asynchronously. This is what lets a front-end *push* a service
    /// request to a client instead of making it poll.
    ///
    /// `None` means this backend has no notification path — distinct from
    /// "subscribed, and no SRQ has happened". Callers must not present it as
    /// the latter.
    fn subscribe_srq(&self) -> Option<tokio::sync::broadcast::Receiver<()>> {
        None
    }

    /// Send raw GPIB command bytes (ATN asserted), ending in standby.
    ///
    /// This is the interface-level primitive under VXI-11.2's Send Command
    /// docmd and the bus-wide control sequences (DCL, GET-without-address):
    /// the caller chooses the bytes, including addressing ones. The default
    /// refuses: a backend that cannot do this must say so, not fake it.
    async fn send_bus_command(&mut self, cmds: &[u8]) -> Result<()> {
        let _ = cmds;
        anyhow::bail!("{} cannot send raw bus commands", self.name())
    }

    /// Drive the ATN line directly: take control (true) or go to standby
    /// (false). VXI-11.2 ATN Control. Default refuses, as above — notably
    /// the 82357 backend has no *verified* raw-ATN path yet.
    async fn set_atn(&mut self, assert: bool) -> Result<()> {
        let _ = assert;
        anyhow::bail!("{} cannot drive ATN directly", self.name())
    }

    /// The controller's own primary address, as configured at init. VXI-11.2
    /// Bus Status selector 8.
    fn controller_pad(&self) -> u8;

    /// Re-address the controller at runtime (VXI-11.2 Bus Address, docmd
    /// 0x02000A): the address-register slice of init, nothing else.
    async fn set_controller_pad(&mut self, pad: u8) -> Result<()>;

    /// Send data bytes with no addressing sequence: IEEE 488.2 16.2.3 SEND
    /// DATA BYTES, for interface links whose client has done its own
    /// addressing via docmd Send Command. The chip must already be in a
    /// state where it may talk; that is the caller's contract.
    async fn send_data_unaddressed(&mut self, data: &[u8], send_eoi: bool) -> Result<()>;

    /// Receive data bytes with no addressing sequence: IEEE 488.2 16.2.6
    /// RECEIVE RESPONSE MESSAGE, the read half of the same contract.
    async fn read_unaddressed(&mut self, max_len: usize) -> Result<(Vec<u8>, bool)>;

    /// Configure the end-of-string terminator used when reading.
    fn set_eos(&mut self, eos_char: u8, enabled: bool);

    /// The current end-of-string configuration, so a front-end that needs a
    /// different terminator for one operation (VXI-11 device_read's termChar)
    /// can put back what another front-end configured — the Prologix `++eos`
    /// state is persistent by that protocol's contract, and a sibling
    /// front-end silently clearing it would break it.
    fn eos(&self) -> (u8, bool);

    /// Set the per-operation GPIB timeout in milliseconds.
    fn set_timeout(&mut self, timeout_ms: u32);

    /// How finely a front-end may slice a long read deadline on this
    /// adapter, if it must slice at all.
    ///
    /// `Some(ms)`: the adapter's timeout hardware is a coarse code table
    /// that rounds up, so a deadline is best enforced by polling in slices
    /// of about this size (the NI table's 300 ms step fits 250). `None`:
    /// the adapter honors milliseconds exactly — hand it the whole
    /// remaining budget in one read. This also matters because a backend's
    /// timeout path may be heavyweight (the 82357 aborts the transfer and
    /// pulses IFC); slicing such an adapter turns every quiet moment into
    /// a bus reset.
    fn read_slice_ms(&self) -> Option<u32> {
        None
    }

    /// Stable identifier for this adapter kind (e.g. `"agilent-82357b"`).
    fn name(&self) -> &'static str;

    /// Leave the adapter in a clean state before the daemon exits.
    ///
    /// Adapters keep their state across host process restarts, so skipping this
    /// can leave hardware that the next session cannot talk to. Best-effort:
    /// failures are logged, not propagated. The default does nothing.
    async fn shutdown(&mut self) -> Result<()> {
        Ok(())
    }
}

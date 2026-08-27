// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// A simulated IEEE-488.2 instrument sitting behind the `GpibBackend` trait.
//
// This is the piece that makes the vendored HiSLIP and VXI-11 servers run
// without a bench. Everything above it — session handling, message framing,
// locking, the status model as the *protocol* sees it — is ugpibd's real
// server code, unmodified. Only the thing at the end of the bus is fake.
//
// That split is the whole point. A mock that reimplements the protocol tests
// the mock; a real server with a fake instrument tests the server, and the
// client talking to it. The faults injected here are the ones an instrument
// produces (a query that never answers, a slow reply, a status byte that
// lies); faults the *transport* produces belong in `faults.rs`, applied at
// the socket, where a client bug cannot be papered over by an obliging
// instrument.
//
// The status model follows IEEE 488.2 §11.2: STB bit 4 (MAV) reflects the
// output queue, bit 5 (ESB) is ESR & ESE, bit 6 (RQS/MSS) is set when any
// other STB bit is enabled in SRE. SRQ is asserted on the 0->1 edge of MSS.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use anyhow::{bail, Result};
use tokio::sync::{broadcast, Mutex};

use crate::backend::GpibBackend;
use crate::faults::Faults;
use crate::observe::{Event, Observed};

/// STB bit 4: the output queue is non-empty (IEEE 488.2 §11.2.2).
const STB_MAV: u8 = 0x10;
/// STB bit 5: an enabled event is set in the standard event status register.
const STB_ESB: u8 = 0x20;
/// STB bit 6: master summary / requesting service.
const STB_MSS: u8 = 0x40;

/// ESR bit 5: command error, e.g. a header the instrument does not implement.
const ESR_CME: u8 = 0x20;
/// ESR bit 4: execution error, e.g. a trigger it cannot honour.
const ESR_EXE: u8 = 0x10;
/// ESR bit 0: operation complete.
const ESR_OPC: u8 = 0x01;

/// Default identity. Deliberately *not* an impersonation of a real model:
/// a check that special-cases on `*IDN?` should not silently match here.
pub const DEFAULT_IDN: &str = "TestGear,VirtualInstrument,0,1.0";

/// How the simulated instrument answers when it has nothing to say.
///
/// A real instrument addressed to talk with an empty output queue does not
/// send a zero-length message — it says nothing at all, and the controller
/// times out. Reproducing that (rather than returning an empty `Vec`) is what
/// makes the timeout paths in the servers and clients testable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Silence {
    /// Time out the way a real bus read does.
    Timeout,
    /// Return no bytes with EOI set. Some checks want the degenerate case.
    EmptyMessage,
}

/// One simulated instrument at one primary address.
pub struct Device {
    pub idn: String,
    /// Output queue. Drives MAV, drained by reads.
    output: VecDeque<u8>,
    /// Bytes received so far for a program message that is not yet complete.
    ///
    /// A GPIB write is not automatically a whole message: the message ends at
    /// EOI or at the EOS character, and a controller may split one across
    /// several writes. Executing each write as if it were complete makes a
    /// client that legitimately splits a message -- which is exactly what
    /// clearing VI_ATTR_SEND_END_EN asks for -- look like it is corrupting
    /// the stream.
    input: Vec<u8>,
    /// SCPI error queue, oldest first (SCPI-99 §21.8).
    errors: VecDeque<(i32, String)>,
    sre: u8,
    ese: u8,
    esr: u8,
    /// User-visible summary bits other than MAV/ESB/MSS, e.g. a questionable
    /// or operation-status summary a check drives directly.
    user_stb: u8,
    /// Last computed MSS, so SRQ fires on the edge and not on every poll.
    last_mss: bool,
    remote: bool,
    lockout: bool,
    /// Scripted answers, consulted before the built-in command set. Lets a
    /// check add an instrument-specific query without touching this file.
    responses: HashMap<String, String>,
    /// Length of the reply to the configured big query, for chunking checks.
    big_reply_len: usize,
    silence: Silence,
    pub triggers: usize,
    pub clears: usize,
}

impl Default for Device {
    fn default() -> Self {
        Self {
            idn: DEFAULT_IDN.to_string(),
            output: VecDeque::new(),
            input: Vec::new(),
            errors: VecDeque::new(),
            sre: 0,
            ese: 0,
            esr: 0,
            user_stb: 0,
            last_mss: false,
            remote: false,
            lockout: false,
            responses: HashMap::new(),
            big_reply_len: 3200,
            silence: Silence::Timeout,
            triggers: 0,
            clears: 0,
        }
    }
}

impl Device {
    /// The status byte as a serial poll would report it.
    pub fn stb(&self) -> u8 {
        let mut stb = self.user_stb;
        if !self.output.is_empty() {
            stb |= STB_MAV;
        }
        if self.esr & self.ese != 0 {
            stb |= STB_ESB;
        }
        // MSS summarises every *other* bit that SRE enables. Bit 6 itself is
        // excluded from the summary (IEEE 488.2 §11.2.1.3), which is why the
        // mask drops it before the test.
        if stb & self.sre & !STB_MSS != 0 {
            stb |= STB_MSS;
        }
        stb
    }

    /// Queue a SCPI error, newest last, and set the matching ESR summary bit.
    fn error(&mut self, code: i32, text: &str) {
        // SCPI-99 §21.8: the queue holds at least 2 entries; when it fills,
        // the last slot is replaced by -350 "Queue overflow". 16 is a common
        // real depth and is what this models.
        if self.errors.len() >= 16 {
            self.errors.pop_back();
            self.errors
                .push_back((-350, "Queue overflow".to_string()));
        } else {
            self.errors.push_back((code, text.to_string()));
        }
        self.esr |= if code <= -200 && code > -300 {
            ESR_EXE
        } else {
            ESR_CME
        };
    }

    fn reply(&mut self, text: &str) {
        self.output.extend(text.as_bytes());
        self.output.push_back(b'\n');
    }

    /// Reset the device to its power-on state, as `*RST` and device clear do.
    /// Device clear empties the queues but leaves the enable registers alone
    /// (IEEE 488.2 §5.6); `*RST` is the fuller reset.
    pub fn clear_io(&mut self) {
        self.output.clear();
        self.input.clear();
    }

    /// Script an answer for `query`, or remove one with `None`.
    pub fn set_response(&mut self, query: &str, response: Option<String>) {
        let key = query.trim().to_ascii_uppercase();
        match response {
            Some(text) => {
                self.responses.insert(key, text);
            }
            None => {
                self.responses.remove(&key);
            }
        }
    }

    pub fn set_big_reply_len(&mut self, bytes: usize) {
        self.big_reply_len = bytes;
    }

    /// Force summary bits into the status byte, so a check can raise SRQ
    /// without needing a measurement to complete first.
    pub fn set_user_stb(&mut self, bits: u8) {
        self.user_stb = bits;
    }

    pub fn set_silence(&mut self, silence: Silence) {
        self.silence = silence;
    }

    pub fn reset(&mut self) {
        self.output.clear();
        self.input.clear();
        self.errors.clear();
        self.esr = 0;
        self.user_stb = 0;
    }
}

/// The bus: a set of simulated instruments, one per primary address.
pub struct VirtualInstrument {
    devices: HashMap<u8, Device>,
    controller_pad: u8,
    eos: (u8, bool),
    timeout_ms: u32,
    ren: bool,
    listen_only: bool,
    device_address: Option<u8>,
    srq_tx: broadcast::Sender<()>,
    srq_line: bool,
    /// Milliseconds a read costs while the device is in local rather than
    /// remote state.
    ///
    /// Real and worth modelling: a GPIB instrument in local services the bus
    /// far more slowly than one in remote -- about 18x on an HP 34401A -- and
    /// that difference is the only way a client can read the remote/local
    /// state back without a human at the front panel. Without it the whole
    /// remote/local matrix is untestable off a bench, which is how a server
    /// treating every REN code as a no-op stayed healthy-looking for a long
    /// time: the calls returned success and nothing happened.
    local_penalty_ms: u64,
    faults: Arc<Faults>,
    observed: Arc<Observed>,
}

impl VirtualInstrument {
    pub fn new(pads: &[u8], faults: Arc<Faults>, observed: Arc<Observed>) -> Self {
        let (srq_tx, _) = broadcast::channel(64);
        let mut devices = HashMap::new();
        for &pad in pads {
            devices.insert(pad, Device::default());
        }
        Self {
            devices,
            controller_pad: 0,
            eos: (b'\n', false),
            timeout_ms: 5000,
            ren: false,
            listen_only: false,
            device_address: None,
            srq_tx,
            srq_line: false,
            local_penalty_ms: 12,
            faults,
            observed,
        }
    }

    /// The shared handle the servers and the control channel both hold.
    ///
    /// Returned as the concrete type on purpose: the servers coerce it to
    /// `Arc<Mutex<dyn GpibBackend>>` at the call site, while the control
    /// channel needs to reach the simulated devices behind the trait. Handing
    /// out the trait object here would mean adding a downcast to the vendored
    /// trait, and keeping that file identical to ugpibd's is worth more.
    pub fn shared(
        pads: &[u8],
        faults: Arc<Faults>,
        observed: Arc<Observed>,
    ) -> Arc<Mutex<Self>> {
        Arc::new(Mutex::new(Self::new(pads, faults, observed)))
    }

    /// Put every simulated device back to its power-on state.
    pub fn reset_devices(&mut self) {
        for dev in self.devices.values_mut() {
            dev.reset();
        }
        self.srq_line = false;
    }

    pub fn device_mut(&mut self, pad: u8) -> Result<&mut Device> {
        self.devices
            .get_mut(&pad)
            .ok_or_else(|| anyhow::anyhow!("no instrument at primary address {pad}"))
    }

    /// Recompute SRQ and fire the broadcast on a rising MSS edge.
    pub fn update_srq(&mut self, pad: u8) {
        let mss = self
            .devices
            .get(&pad)
            .map(|d| d.stb() & STB_MSS != 0)
            .unwrap_or(false);
        if let Some(dev) = self.devices.get_mut(&pad) {
            let rising = mss && !dev.last_mss;
            dev.last_mss = mss;
            if rising {
                self.srq_line = true;
                let _ = self.srq_tx.send(());
            }
        }
        // The line follows *any* device requesting service, not just this one.
        self.srq_line = self.devices.values().any(|d| d.stb() & STB_MSS != 0);
    }

    /// Execute one parsed program message unit against the addressed device.
    fn execute(&mut self, pad: u8, unit: &str) -> Result<()> {
        let unit = unit.trim();
        if unit.is_empty() {
            return Ok(());
        }
        let (head, arg) = match unit.split_once(char::is_whitespace) {
            Some((h, a)) => (h.trim(), a.trim()),
            None => (unit, ""),
        };
        let upper = head.to_ascii_uppercase();

        // Scripted answers win, so a check can define a query this file has
        // never heard of without editing it.
        if let Some(dev) = self.devices.get(&pad) {
            if let Some(canned) = dev.responses.get(&upper).cloned() {
                if let Some(dev) = self.devices.get_mut(&pad) {
                    dev.reply(&canned);
                }
                return Ok(());
            }
        }

        let big_len = self.devices.get(&pad).map(|d| d.big_reply_len).unwrap_or(0);
        let idn = self.devices.get(&pad).map(|d| d.idn.clone()).unwrap_or_default();
        let dev = self.device_mut(pad)?;

        match upper.as_str() {
            "*IDN?" => dev.reply(&idn),
            "*RST" => dev.reset(),
            "*CLS" => {
                // *CLS clears the status structure but not the enable
                // registers (IEEE 488.2 §10.3), and does not touch the
                // output queue unless it is the first thing in the message.
                dev.errors.clear();
                dev.esr = 0;
                dev.user_stb = 0;
            }
            "*STB?" => {
                let stb = dev.stb();
                dev.reply(&stb.to_string());
            }
            "*SRE" => match arg.parse::<u8>() {
                Ok(v) => dev.sre = v,
                Err(_) => dev.error(-222, "Data out of range"),
            },
            "*SRE?" => {
                let v = dev.sre;
                dev.reply(&v.to_string());
            }
            "*ESE" => match arg.parse::<u8>() {
                Ok(v) => dev.ese = v,
                Err(_) => dev.error(-222, "Data out of range"),
            },
            "*ESE?" => {
                let v = dev.ese;
                dev.reply(&v.to_string());
            }
            "*ESR?" => {
                // Reading ESR clears it (IEEE 488.2 §11.5.1.2.2).
                let v = dev.esr;
                dev.esr = 0;
                dev.reply(&v.to_string());
            }
            "*OPC" => dev.esr |= ESR_OPC,
            "*OPC?" => dev.reply("1"),
            "*TST?" => dev.reply("0"),
            "*WAI" => {}
            "*TRG" => {
                dev.triggers += 1;
                dev.error(-211, "Trigger ignored");
            }
            "SYST:ERR?" | "SYSTEM:ERROR?" | "SYST:ERR:NEXT?" => {
                let entry = dev
                    .errors
                    .pop_front()
                    .unwrap_or((0, "No error".to_string()));
                dev.reply(&format!("{},\"{}\"", entry.0, entry.1));
            }
            // A deliberately long, byte-for-byte repeatable reply, for the
            // multi-chunk read checks. Repeatable matters: a reply that
            // differs between reads while matching in length looks exactly
            // like a transport fault and is not one.
            "TEST:BIG?" => {
                let body: String = (0..big_len)
                    .map(|i| char::from(b'0' + (i % 10) as u8))
                    .collect();
                dev.reply(&body);
            }
            "TEST:SRQ" => {
                dev.user_stb |= 0x01;
            }
            "TEST:SILENT?" => {
                // Accepted, answered with nothing: the instrument addressed
                // to talk with an empty queue.
            }
            _ => {
                if upper.ends_with('?') {
                    dev.error(-113, &format!("Undefined header;{head}"));
                } else {
                    dev.error(-113, &format!("Undefined header;{head}"));
                }
            }
        }
        Ok(())
    }
}

#[async_trait::async_trait]
impl GpibBackend for VirtualInstrument {
    async fn init(&mut self, my_pad: u8) -> Result<()> {
        self.controller_pad = my_pad;
        Ok(())
    }

    async fn write(&mut self, pad: u8, data: &[u8], send_eoi: bool) -> Result<()> {
        self.observed
            .push(Event::Write { pad, data: data.to_vec(), eoi: send_eoi });
        self.faults.before_write(data).await?;

        if !self.devices.contains_key(&pad) {
            bail!("no instrument at primary address {pad}");
        }

        // Accumulate until the message is actually terminated. EOI ends it;
        // so does a newline, which is how an instrument with EOS enabled sees
        // the end of a message that carried no EOI.
        let complete = send_eoi || data.contains(&b'\n');
        {
            let dev = self.device_mut(pad)?;
            dev.input.extend_from_slice(data);
            if !complete {
                return Ok(());
            }
        }

        let message = {
            let dev = self.device_mut(pad)?;
            std::mem::take(&mut dev.input)
        };
        let text = String::from_utf8_lossy(&message).to_string();
        // IEEE 488.2 §7.6.2: a program message is units separated by ';'.
        for unit in text.trim_end_matches(['\n', '\r']).split(';') {
            self.execute(pad, unit)?;
        }
        self.update_srq(pad);
        Ok(())
    }

    async fn read(&mut self, pad: u8, max_len: usize) -> Result<(Vec<u8>, bool)> {
        self.faults.before_read().await?;

        let silence = self
            .devices
            .get(&pad)
            .map(|d| d.silence)
            .unwrap_or(Silence::Timeout);
        let (eos_char, eos_on) = self.eos;
        let penalty = self.local_penalty_ms;
        let in_local = !self.devices.get(&pad).map(|d| d.remote).unwrap_or(false);
        if in_local && penalty > 0 {
            tokio::time::sleep(std::time::Duration::from_millis(penalty)).await;
        }
        let dev = self.device_mut(pad)?;

        if dev.output.is_empty() {
            self.observed.push(Event::ReadTimeout { pad });
            return match silence {
                // No data and no END is how a real adapter reports a bus
                // timeout, and it is what the servers above are written
                // against: they enforce the client's io_timeout themselves,
                // in short slices, because an adapter's own timeout table
                // never sees the client's number.
                //
                // Sleeping here instead would be worse than merely
                // redundant. The bus mutex is held across this call, so a
                // sleep blocks *every other session* for its duration --
                // which presents as unrelated checks timing out, and reads
                // exactly like a client-side concurrency bug.
                Silence::Timeout => Ok((Vec::new(), false)),
                Silence::EmptyMessage => Ok((Vec::new(), true)),
            };
        }

        // Stop at the EOS character when one is armed. A real adapter ends the
        // read on it, which is what lets a client ask for one line of a
        // multi-line reply and come back for the rest; a backend that ignores
        // eos hands the whole thing over in one read and makes the client look
        // like it is not honouring VI_ATTR_TERMCHAR.
        let mut take = max_len.min(dev.output.len());
        if eos_on {
            if let Some(at) = dev.output.iter().take(take).position(|b| *b == eos_char) {
                take = at + 1;
            }
        }
        let out: Vec<u8> = dev.output.drain(..take).collect();
        // EOI rides the last byte of the message. If the queue still holds
        // bytes, this was a partial read and EOI stays low, which is what
        // drives the servers' chunking paths -- and is also what distinguishes
        // a read that stopped on the EOS character from one that reached the
        // end of the message.
        let eoi = dev.output.is_empty();
        self.update_srq(pad);
        self.observed.push(Event::Read { pad, data: out.clone(), eoi });
        Ok((out, eoi))
    }

    async fn device_clear(&mut self, pad: u8) -> Result<()> {
        self.observed.push(Event::Clear { pad });
        let dev = self.device_mut(pad)?;
        dev.clears += 1;
        dev.clear_io();
        self.update_srq(pad);
        Ok(())
    }

    async fn trigger(&mut self, pad: u8) -> Result<()> {
        self.observed.push(Event::Trigger { pad });
        let dev = self.device_mut(pad)?;
        dev.triggers += 1;
        // A device whose trigger source is not BUS evaluates the GET and
        // complains, which is how a real 34401A behaves and is itself proof
        // the trigger arrived.
        dev.error(-211, "Trigger ignored");
        self.update_srq(pad);
        Ok(())
    }

    async fn ifc(&mut self) -> Result<()> {
        self.observed.push(Event::Ifc);
        for dev in self.devices.values_mut() {
            dev.remote = false;
        }
        Ok(())
    }

    async fn ren(&mut self, enable: bool) -> Result<()> {
        self.observed.push(Event::Ren { enabled: enable });
        self.ren = enable;
        // Modelled as sticky: asserting REN puts the device in remote and
        // dropping it returns the device to local, and neither is undone by
        // the next thing that addresses it.
        //
        // On real hardware a device enters remote only once REN is asserted
        // *and* it is addressed to listen (IEEE 488.1), so a GTL with REN
        // still high bounces back to remote the moment anything addresses it.
        // Modelling that faithfully makes the state unobservable from a
        // client, because the query used to observe it is itself the
        // addressing -- the suite documents exactly this for `address_gtl`.
        // Reproducing the bounce here would only add noise to an oracle that
        // has to distinguish the two states.
        for dev in self.devices.values_mut() {
            dev.remote = enable;
            if !enable {
                dev.lockout = false;
            }
        }
        Ok(())
    }

    async fn go_to_remote(&mut self, pad: u8) -> Result<()> {
        self.observed.push(Event::GoToRemote { pad });
        self.ren = true;
        self.device_mut(pad)?.remote = true;
        Ok(())
    }

    async fn go_to_local(&mut self, pad: u8) -> Result<()> {
        self.observed.push(Event::GoToLocal { pad });
        self.device_mut(pad)?.remote = false;
        Ok(())
    }

    async fn local_lockout(&mut self) -> Result<()> {
        self.observed.push(Event::LocalLockout);
        for dev in self.devices.values_mut() {
            dev.lockout = true;
        }
        Ok(())
    }

    async fn serial_poll(&mut self, pad: u8) -> Result<u8> {
        if let Some(forced) = self.faults.forced_stb() {
            self.observed.push(Event::SerialPoll { pad, stb: forced });
            return Ok(forced);
        }
        let stb = self.device_mut(pad)?.stb();
        // A serial poll clears RQS (but not MSS) on a real device; modelling
        // that as "the SRQ line drops for this device" is enough here.
        self.device_mut(pad)?.last_mss = stb & STB_MSS != 0;
        self.srq_line = self.devices.values().any(|d| d.stb() & STB_MSS != 0);
        self.observed.push(Event::SerialPoll { pad, stb });
        Ok(stb)
    }

    async fn srq_asserted(&mut self) -> Result<bool> {
        Ok(self.srq_line)
    }

    fn subscribe_srq(&self) -> Option<broadcast::Receiver<()>> {
        Some(self.srq_tx.subscribe())
    }

    async fn set_listen_only(&mut self, enable: bool) -> Result<()> {
        self.listen_only = enable;
        Ok(())
    }

    fn listen_only(&self) -> bool {
        self.listen_only
    }

    async fn set_device_mode(&mut self, address: Option<u8>) -> Result<()> {
        self.device_address = address;
        Ok(())
    }

    fn device_address(&self) -> Option<u8> {
        self.device_address
    }

    fn controller_pad(&self) -> u8 {
        self.controller_pad
    }

    async fn set_controller_pad(&mut self, pad: u8) -> Result<()> {
        self.controller_pad = pad;
        Ok(())
    }

    async fn send_data_unaddressed(&mut self, data: &[u8], send_eoi: bool) -> Result<()> {
        self.observed
            .push(Event::UnaddressedWrite { data: data.to_vec(), eoi: send_eoi });
        Ok(())
    }

    async fn read_unaddressed(&mut self, _max_len: usize) -> Result<(Vec<u8>, bool)> {
        self.observed.push(Event::UnaddressedRead);
        Ok((Vec::new(), false))
    }

    fn set_eos(&mut self, eos_char: u8, enabled: bool) {
        self.eos = (eos_char, enabled);
    }

    fn eos(&self) -> (u8, bool) {
        self.eos
    }

    fn set_timeout(&mut self, timeout_ms: u32) {
        self.timeout_ms = timeout_ms;
    }

    fn name(&self) -> &'static str {
        "virtual"
    }
}

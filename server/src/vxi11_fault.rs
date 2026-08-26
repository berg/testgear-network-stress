// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// VXI-11 fault injection at the RPC layer, done in the proxy.
//
// The transport knobs in `faults.rs` work on bytes and do not need to know
// what they are carrying. These do: "answer the next device_read with error 4"
// or "report a maxRecvSize of zero" are statements about ONC RPC records, so
// something has to parse them.
//
// Doing it here rather than in the vendored server keeps that server
// byte-identical to ugpibd's, which is the whole reason it is trustworthy as
// an oracle. It also means the record this emits is assembled by hand, with
// `struct`-level control over framing -- so a client bug cannot hide behind a
// server that would have refused to emit the malformed thing in the first
// place.
//
// Reference: RFC 5531 for the RPC message form, VXI-11 Rev 1.0 B.5.2 for the
// error codes and B.6.x for the reply structures.

use std::collections::HashMap;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

/// Procedures worth naming (VXI-11 B.6).
pub const CREATE_LINK: u32 = 10;
pub const DEVICE_WRITE: u32 = 11;
pub const DEVICE_READ: u32 = 12;
pub const DEVICE_READSTB: u32 = 13;
pub const DEVICE_LOCK: u32 = 18;

/// One scripted interference with a reply.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Vxi11Faults {
    /// Replace the error field of the next reply to this procedure.
    pub error_on_proc: Option<u32>,
    /// The error code to put there (B.5.2).
    pub error_code: Option<u32>,
    /// Apply it once and then stop, rather than to every reply.
    pub error_once: Option<bool>,
    /// Report this maxRecvSize in the create_link reply. Zero is the
    /// interesting value: a client that divides by it wedges.
    pub max_recv_size: Option<u32>,
    /// Emit a reply carrying an xid nobody is waiting for, immediately before
    /// the real reply to this procedure. This is what an interrupted call
    /// leaves behind in the socket, and a client that matches replies by
    /// arrival order rather than by xid consumes it as the answer to the
    /// wrong question.
    pub stale_reply_before_proc: Option<u32>,
}

impl Vxi11Faults {
    fn is_armed(&self) -> bool {
        self.error_on_proc.is_some()
            || self.max_recv_size.is_some()
            || self.stale_reply_before_proc.is_some()
    }
}

/// Tracks which procedure each in-flight xid belongs to.
///
/// A reply carries an xid and no procedure number, so injecting "an error on
/// device_read" means remembering what the client asked. Replies that arrive
/// for an xid this never saw are passed through untouched rather than guessed
/// at.
#[derive(Default)]
pub struct Tracker {
    calls: Mutex<HashMap<u32, u32>>,
    faults: Mutex<Vxi11Faults>,
}

impl Tracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self, faults: Vxi11Faults) {
        *self.faults.lock().unwrap() = faults;
    }

    pub fn get(&self) -> Vxi11Faults {
        self.faults.lock().unwrap().clone()
    }

    pub fn clear(&self) {
        *self.faults.lock().unwrap() = Vxi11Faults::default();
        self.calls.lock().unwrap().clear();
    }

    /// Note the procedure of every call going to the server.
    ///
    /// A buffer that does not parse is ignored rather than guessed at. The
    /// cost of missing a call is that a fault does not fire; the cost of
    /// mis-parsing one is firing it on the wrong reply.
    pub fn observe_calls(&self, buf: &[u8]) {
        let Some(spans) = whole_records(buf) else {
            return;
        };
        for (_, body_at, body_len) in spans {
            if let Some((xid, proc)) = parse_call(&buf[body_at..body_at + body_len]) {
                self.calls.lock().unwrap().insert(xid, proc);
            }
        }
    }

    /// Rewrite replies coming back, if anything is armed.
    ///
    /// Returns None when nothing applies, so the common path forwards the
    /// original buffer untouched. None is also the answer whenever the buffer
    /// does not parse cleanly into whole records: a reply split across two
    /// reads, or anything unexpected on the wire, must be forwarded byte for
    /// byte rather than reassembled on a guess. Getting that wrong does not
    /// produce a wrong answer, it produces a hang -- the client waits forever
    /// for bytes the proxy quietly dropped.
    pub fn rewrite_replies(&self, buf: &[u8]) -> Option<Vec<u8>> {
        let faults = self.faults.lock().unwrap().clone();
        if !faults.is_armed() {
            return None;
        }

        let spans = whole_records(buf)?;
        let mut out: Vec<u8> = Vec::with_capacity(buf.len() + 64);
        let mut changed = false;

        for (mark_at, body_at, body_len) in spans {
            let body = &buf[body_at..body_at + body_len];
            let xid = match read_u32(body, 0) {
                Some(x) => x,
                None => {
                    out.extend_from_slice(&buf[mark_at..body_at + body_len]);
                    continue;
                }
            };
            let proc = self.calls.lock().unwrap().get(&xid).copied();
            let mut body = body.to_vec();

            // Results begin after: xid, msg_type, reply_stat, verifier
            // flavour, verifier length (5 words), plus accept_stat.
            let results = 4 * 6;

            if let (Some(target), Some(code)) = (faults.error_on_proc, faults.error_code) {
                if proc == Some(target) && write_u32(&mut body, results, code) {
                    changed = true;
                    if faults.error_once.unwrap_or(true) {
                        self.faults.lock().unwrap().error_on_proc = None;
                    }
                }
            }

            if let Some(size) = faults.max_recv_size {
                // create_link reply: error, lid, abortPort, maxRecvSize --
                // the fourth result word.
                if proc == Some(CREATE_LINK) && write_u32(&mut body, results + 12, size) {
                    changed = true;
                }
            }

            if let Some(target) = faults.stale_reply_before_proc {
                if proc == Some(target) {
                    // An xid far from anything in flight, so a client that
                    // checks cannot mistake it for a real one -- the whole
                    // question is whether it checks.
                    out.extend_from_slice(&frame(&stale_body(xid ^ 0x5A5A_5A5A)));
                    changed = true;
                    self.faults.lock().unwrap().stale_reply_before_proc = None;
                }
            }

            // The original record mark is preserved rather than rebuilt: the
            // body length is unchanged by every rewrite here, and reframing
            // would quietly drop the fragment bit.
            out.extend_from_slice(&buf[mark_at..body_at]);
            out.extend_from_slice(&body);
        }

        if changed {
            Some(out)
        } else {
            None
        }
    }
}

/// Every record in `buf` as `(mark offset, body offset, body length)`, or None
/// if the buffer does not divide exactly into whole records.
fn whole_records(buf: &[u8]) -> Option<Vec<(usize, usize, usize)>> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < buf.len() {
        if pos + 4 > buf.len() {
            return None;
        }
        let mark = u32::from_be_bytes([buf[pos], buf[pos + 1], buf[pos + 2], buf[pos + 3]]);
        let len = (mark & 0x7FFF_FFFF) as usize;
        if len == 0 || pos + 4 + len > buf.len() {
            return None;
        }
        out.push((pos, pos + 4, len));
        pos += 4 + len;
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

/// A successful-looking reply body carrying `xid` and a VXI-11 error 0.
fn stale_body(xid: u32) -> Vec<u8> {
    let mut body = Vec::new();
    body.extend_from_slice(&xid.to_be_bytes());
    body.extend_from_slice(&1u32.to_be_bytes()); // msg_type = REPLY
    body.extend_from_slice(&0u32.to_be_bytes()); // reply_stat = MSG_ACCEPTED
    body.extend_from_slice(&0u32.to_be_bytes()); // verifier flavour = AUTH_NONE
    body.extend_from_slice(&0u32.to_be_bytes()); // verifier length
    body.extend_from_slice(&0u32.to_be_bytes()); // accept_stat = SUCCESS
    body.extend_from_slice(&0u32.to_be_bytes()); // error = no error
    body.extend_from_slice(&0u32.to_be_bytes()); // reason
    body.extend_from_slice(&0u32.to_be_bytes()); // zero-length data
    body
}

/// Wrap a body in a last-fragment record mark.
fn frame(body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(body.len() + 4);
    out.extend_from_slice(&((body.len() as u32) | 0x8000_0000).to_be_bytes());
    out.extend_from_slice(body);
    out
}

/// `(xid, proc)` for an RPC call record.
fn parse_call(record: &[u8]) -> Option<(u32, u32)> {
    let xid = read_u32(record, 0)?;
    if read_u32(record, 4)? != 0 {
        return None; // not a CALL
    }
    let proc = read_u32(record, 20)?;
    Some((xid, proc))
}

fn read_u32(buf: &[u8], at: usize) -> Option<u32> {
    if at + 4 > buf.len() {
        return None;
    }
    Some(u32::from_be_bytes([
        buf[at],
        buf[at + 1],
        buf[at + 2],
        buf[at + 3],
    ]))
}

fn write_u32(buf: &mut [u8], at: usize, value: u32) -> bool {
    if at + 4 > buf.len() {
        return false;
    }
    buf[at..at + 4].copy_from_slice(&value.to_be_bytes());
    true
}

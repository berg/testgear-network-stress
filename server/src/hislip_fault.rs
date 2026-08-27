// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// HiSLIP fault injection and observation at the message layer, done in the
// proxy — the counterpart to `vxi11_fault.rs`.
//
// IVI-6.1 section 3.1.2 lists what a HiSLIP client SHALL do, and almost all of
// it concerns MessageIDs: verify the one on an incoming DataEND against the one
// last sent, discard the message and clear buffered responses when they differ,
// and maintain a counter that starts at 0xFFFFFF00 and steps by two. None of
// that is visible through the VISA API. It is only visible on the wire, which
// is why it lives here.
//
// Two capabilities, and the cheap one matters as much as the loud one:
//
// - *Observation*: record every message header in both directions, so a check
//   can assert on the MessageID sequence a client actually emitted. This needs
//   no injection at all and covers the counter rule outright.
//
// - *Injection*: rewrite the Message Parameter of a server DataEND, so the
//   client is handed a MessageID that does not match what it asked for and has
//   to do the thing 3.1.2 rule 1 requires.
//
// Reference: IVI-6.1 Table 2 (header format), Table 4 (message type values),
// section 3.1.2 (client requirements).

use std::sync::Mutex;

use serde::{Deserialize, Serialize};

/// IVI-6.1 Table 2: every HiSLIP message opens with this fixed-size header.
pub const HEADER_LEN: usize = 16;
/// The prologue, ASCII "HS", most significant byte first.
pub const PROLOGUE: [u8; 2] = *b"HS";

// IVI-6.1 Table 4, the subset this needs to recognise.
pub const MSG_FATAL_ERROR: u8 = 2;
pub const MSG_ERROR: u8 = 3;
pub const MSG_DATA: u8 = 6;
pub const MSG_DATA_END: u8 = 7;
pub const MSG_TRIGGER: u8 = 12;
pub const MSG_INTERRUPTED: u8 = 13;

/// The MessageID a client starts from (IVI-6.1 3.1.2).
pub const INITIAL_MESSAGE_ID: u32 = 0xFFFF_FF00;
/// The step it advances by, in an unsigned 32-bit sense.
pub const MESSAGE_ID_STEP: u32 = 2;

/// One message header seen on the wire.
#[derive(Clone, Debug, Serialize)]
pub struct Seen {
    /// "client" for client->server, "server" for the other direction.
    pub from: &'static str,
    pub message_type: u8,
    pub control_code: u8,
    pub message_parameter: u32,
    pub payload_len: u64,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HislipFaults {
    /// Add this to the Message Parameter of the next server DataEND, so the
    /// MessageID no longer matches what the client sent. IVI-6.1 3.1.2 rule 1
    /// requires the client to discard the message and clear its buffers.
    pub skew_data_end_id: Option<u32>,
    /// Same, for the next Data message (3.1.2 rule 2).
    pub skew_data_id: Option<u32>,
    /// Corrupt the prologue of the next server message. Section 2.3 gives the
    /// prologue exactly this job -- letting a device notice it is out of sync
    /// -- so a client that ignores it is ignoring its only framing check.
    pub break_prologue: Option<bool>,
    /// Split every server DataEND into a Data of this many payload bytes
    /// followed by a DataEND carrying the rest.
    ///
    /// The server chunks its replies only when one exceeds the maximum the
    /// client declared, and a client declaring a megabyte never provokes it --
    /// so `Data` messages simply never occur, and IVI-6.1 3.1.2 rule 2, which
    /// is entirely about receiving one, could not be tested at all.
    ///
    /// This produces a genuinely chunked reply rather than a malformed one:
    /// both messages carry the same MessageID, which is what the server itself
    /// emits when the client's maximum is small. It is a *shape* knob, not a
    /// fault, and it is what makes the rule-2 fault reachable.
    pub split_data_end_at: Option<usize>,
}

impl HislipFaults {
    fn armed(&self) -> bool {
        self.skew_data_end_id.is_some()
            || self.skew_data_id.is_some()
            || self.break_prologue.unwrap_or(false)
            || self.split_data_end_at.is_some()
    }
}

#[derive(Default)]
pub struct Tracker {
    faults: Mutex<HislipFaults>,
    seen: Mutex<Vec<Seen>>,
    /// Payload bytes still expected in each direction.
    ///
    /// A message larger than one read arrives as a header plus continuation
    /// chunks that are pure payload. Without carrying that count across
    /// chunks, every continuation looks like a malformed header and the
    /// message is neither recorded nor injectable -- which silently turned a
    /// large-reply check into one that could never fire, and reported the
    /// result as a finding.
    pending: Mutex<(u64, u64)>,
}

fn take_pending(slot: &mut u64, available: usize) -> usize {
    let consumed = (*slot).min(available as u64);
    *slot -= consumed;
    consumed as usize
}

impl Tracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self, faults: HislipFaults) {
        *self.faults.lock().unwrap() = faults;
    }

    pub fn clear(&self) {
        *self.faults.lock().unwrap() = HislipFaults::default();
        self.seen.lock().unwrap().clear();
    }

    pub fn snapshot(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }

    /// Record the headers in a buffer travelling in one direction.
    pub fn observe(&self, buf: &[u8], from: &'static str) {
        let start = {
            let mut pending = self.pending.lock().unwrap();
            let slot = if from == "client" {
                &mut pending.0
            } else {
                &mut pending.1
            };
            take_pending(slot, buf.len())
        };
        let (found, leftover) = scan(&buf[start..]);
        for header in found {
            self.seen.lock().unwrap().push(Seen {
                from,
                message_type: header.message_type,
                control_code: header.control_code,
                message_parameter: header.message_parameter,
                payload_len: header.payload_len,
            });
        }
        let mut pending = self.pending.lock().unwrap();
        if from == "client" {
            pending.0 = leftover;
        } else {
            pending.1 = leftover;
        }
    }

    /// Rewrite server->client messages if anything is armed.
    ///
    /// As with the VXI-11 tracker, a buffer that does not divide exactly into
    /// whole messages is left alone rather than reassembled on a guess: losing
    /// bytes in a proxy does not produce a wrong answer, it produces a hang.
    pub fn rewrite(&self, buf: &[u8]) -> Option<Vec<u8>> {
        let faults = self.faults.lock().unwrap().clone();
        if !faults.armed() {
            return None;
        }
        // Only the portion after any in-flight payload can hold a header.
        let start = { self.pending.lock().unwrap().1 }.min(buf.len() as u64) as usize;
        let found = headers(&buf[start..]);
        if found.is_empty() {
            return None;
        }

        // Splitting changes the buffer's length, so it is done first and on
        // its own: rewriting offsets computed against the original buffer
        // after inserting a message would corrupt every later one.
        if let Some(cut) = faults.split_data_end_at {
            if let Some(split) = split_first_data_end(buf, start, cut) {
                self.faults.lock().unwrap().split_data_end_at = None;
                // Re-enter so an armed skew still lands, now that there is a
                // Data message for it to land on.
                return Some(self.rewrite(&split).unwrap_or(split));
            }
        }

        let mut out = buf.to_vec();
        let mut changed = false;
        for (rel, header) in found {
            let at = start + rel;
            if let Some(skew) = faults.skew_data_end_id {
                if header.message_type == MSG_DATA_END {
                    let skewed = header.message_parameter.wrapping_add(skew);
                    out[at + 4..at + 8].copy_from_slice(&skewed.to_be_bytes());
                    changed = true;
                    self.faults.lock().unwrap().skew_data_end_id = None;
                }
            }
            if let Some(skew) = faults.skew_data_id {
                if header.message_type == MSG_DATA {
                    let skewed = header.message_parameter.wrapping_add(skew);
                    out[at + 4..at + 8].copy_from_slice(&skewed.to_be_bytes());
                    changed = true;
                    self.faults.lock().unwrap().skew_data_id = None;
                }
            }
            if faults.break_prologue.unwrap_or(false) {
                out[at] = b'X';
                changed = true;
                self.faults.lock().unwrap().break_prologue = Some(false);
            }
        }

        if changed {
            Some(out)
        } else {
            None
        }
    }
}

/// Rewrite the first DataEND at or after `start` as Data + DataEND.
///
/// Returns None when there is no DataEND with more than `cut` payload bytes,
/// so a caller can tell "nothing to split" from "split".
fn split_first_data_end(buf: &[u8], start: usize, cut: usize) -> Option<Vec<u8>> {
    if cut == 0 {
        return None;
    }
    for (rel, header) in headers(&buf[start..]) {
        if header.message_type != MSG_DATA_END {
            continue;
        }
        let payload_len = header.payload_len as usize;
        if payload_len <= cut {
            continue;
        }
        let at = start + rel;
        let body = at + HEADER_LEN;
        let end = body + payload_len;
        if end > buf.len() {
            return None; // still arriving; leave it alone
        }

        let mut out = Vec::with_capacity(buf.len() + HEADER_LEN);
        out.extend_from_slice(&buf[..at]);
        out.extend_from_slice(&message(
            MSG_DATA,
            header.control_code,
            header.message_parameter,
            &buf[body..body + cut],
        ));
        out.extend_from_slice(&message(
            MSG_DATA_END,
            header.control_code,
            header.message_parameter,
            &buf[body + cut..end],
        ));
        out.extend_from_slice(&buf[end..]);
        return Some(out);
    }
    None
}

/// One HiSLIP message, header and payload (IVI-6.1 Table 2).
fn message(message_type: u8, control_code: u8, parameter: u32, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER_LEN + payload.len());
    out.extend_from_slice(&PROLOGUE);
    out.push(message_type);
    out.push(control_code);
    out.extend_from_slice(&parameter.to_be_bytes());
    out.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    out.extend_from_slice(payload);
    out
}

struct Header {
    message_type: u8,
    control_code: u8,
    message_parameter: u32,
    payload_len: u64,
}

/// Headers in `buf`, plus payload bytes still owed past its end.
fn scan(buf: &[u8]) -> (Vec<Header>, u64) {
    let mut out = Vec::new();
    let mut pos = 0usize;
    loop {
        if pos + HEADER_LEN > buf.len() {
            return (out, 0);
        }
        if buf[pos] != PROLOGUE[0] || buf[pos + 1] != PROLOGUE[1] {
            return (out, 0);
        }
        let header = read_header(buf, pos);
        let payload = header.payload_len;
        out.push(header);
        let end = pos as u64 + HEADER_LEN as u64 + payload;
        if end > buf.len() as u64 {
            return (out, end - buf.len() as u64);
        }
        pos = end as usize;
    }
}

fn read_header(buf: &[u8], pos: usize) -> Header {
    Header {
        message_type: buf[pos + 2],
        control_code: buf[pos + 3],
        message_parameter: u32::from_be_bytes([
            buf[pos + 4],
            buf[pos + 5],
            buf[pos + 6],
            buf[pos + 7],
        ]),
        payload_len: u64::from_be_bytes([
            buf[pos + 8],
            buf[pos + 9],
            buf[pos + 10],
            buf[pos + 11],
            buf[pos + 12],
            buf[pos + 13],
            buf[pos + 14],
            buf[pos + 15],
        ]),
    }
}

/// Every `(offset, header)` in the buffer, walking payload lengths.
///
/// Stops at the first thing that does not look like a header, which keeps a
/// partially-received message from being mis-parsed as a whole one.
fn headers(buf: &[u8]) -> Vec<(usize, Header)> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos + HEADER_LEN <= buf.len() {
        if buf[pos] != PROLOGUE[0] || buf[pos + 1] != PROLOGUE[1] {
            break;
        }
        let message_parameter =
            u32::from_be_bytes([buf[pos + 4], buf[pos + 5], buf[pos + 6], buf[pos + 7]]);
        let payload_len = u64::from_be_bytes([
            buf[pos + 8],
            buf[pos + 9],
            buf[pos + 10],
            buf[pos + 11],
            buf[pos + 12],
            buf[pos + 13],
            buf[pos + 14],
            buf[pos + 15],
        ]);
        out.push((
            pos,
            Header {
                message_type: buf[pos + 2],
                control_code: buf[pos + 3],
                message_parameter,
                payload_len,
            },
        ));
        // A payload running past the buffer means the message is still
        // arriving. Its header is real and worth keeping -- it is the only
        // place the MessageID appears -- but nothing follows it here.
        let step = HEADER_LEN as u64 + payload_len;
        if step > (buf.len() - pos) as u64 {
            break;
        }
        pos += step as usize;
    }
    out
}

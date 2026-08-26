// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// What the simulated instrument saw, so a check can assert on what the client
// actually did rather than only on what it got back.
//
// The distinction matters more than it sounds. "The query returned the right
// answer" and "the client sent one write and one read in the right order" are
// different claims, and a transport bug can satisfy the first while violating
// the second — a duplicated write that the instrument tolerates, a status
// query smuggled into the middle of a message, a device clear that never
// reached the bus. Those are exactly the bugs this suite exists to find, and
// they are invisible from the client side.

use std::sync::Mutex;

use serde::Serialize;

/// One thing the instrument was asked to do.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Event {
    Write {
        pad: u8,
        #[serde(with = "bytes_as_text")]
        data: Vec<u8>,
        eoi: bool,
    },
    Read {
        pad: u8,
        #[serde(with = "bytes_as_text")]
        data: Vec<u8>,
        eoi: bool,
    },
    /// An addressed read that found the output queue empty. Recorded rather
    /// than inferred from a missing Read, because "the client asked and got
    /// nothing" and "the client never asked" are different bugs.
    ReadTimeout {
        pad: u8,
    },
    SerialPoll {
        pad: u8,
        stb: u8,
    },
    Trigger {
        pad: u8,
    },
    Clear {
        pad: u8,
    },
    Ren {
        enabled: bool,
    },
    GoToRemote {
        pad: u8,
    },
    GoToLocal {
        pad: u8,
    },
    LocalLockout,
    Ifc,
    UnaddressedWrite {
        #[serde(with = "bytes_as_text")]
        data: Vec<u8>,
        eoi: bool,
    },
    UnaddressedRead,
}

/// Bytes are rendered as a lossy string in the JSON the control channel
/// serves. These are SCPI messages: a check that has to eyeball the log wants
/// `*IDN?`, not a list of integers. Non-UTF-8 bytes become replacement
/// characters, which is acceptable because a check asserting on binary
/// payloads should be asserting on lengths, not on the text.
mod bytes_as_text {
    use serde::Serializer;

    pub fn serialize<S: Serializer>(bytes: &[u8], s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&String::from_utf8_lossy(bytes))
    }
}

/// The append-only log of instrument-side events.
#[derive(Default)]
pub struct Observed {
    events: Mutex<Vec<Event>>,
}

impl Observed {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&self, event: Event) {
        self.events.lock().unwrap().push(event);
    }

    pub fn snapshot(&self) -> Vec<Event> {
        self.events.lock().unwrap().clone()
    }

    /// Drop everything recorded so far. A check calls this between phases so
    /// its assertions are about its own traffic and not the session setup's.
    pub fn clear(&self) {
        self.events.lock().unwrap().clear();
    }

    pub fn len(&self) -> usize {
        self.events.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

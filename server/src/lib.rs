// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 ugpibd contributors
// Copyright (C) 2026 testgear-network-stress contributors
//
// A HiSLIP and VXI-11 server that needs no instrument, for testing VISA
// *clients*.
//
// The protocol implementations in `hislip/` and `vxi11/`, along with
// `frontend/` and the `backend` trait, are vendored verbatim from ugpibd
// (GPL-3.0-or-later) — a daemon that serves real GPIB hardware over these same
// protocols and is exercised against real instruments and real VISA clients.
// Vendoring rather than reimplementing is the point: a mock written alongside
// the checks tends to encode the same misreading of the spec twice, and then
// agrees with itself. This code has an independent reason to be correct.
//
// What this crate adds is everything needed to run that server with no bench:
//
// - `virtual_instrument`: a simulated IEEE-488.2 device behind the backend
//   trait, so the servers have something to talk to.
// - `faults`: the knobs, split into instrument-side and transport-side.
// - `proxy`: transport faults applied at the socket, in front of an unmodified
//   server, so the vendored code stays re-vendorable.
// - `observe`: what the instrument saw, so checks can assert on what the
//   client actually sent and not only on what came back.
// - `control`: a line-JSON socket the harness drives all of that from.

pub mod backend;
pub mod control;
pub mod faults;
pub mod frontend;
pub mod hislip;
pub mod observe;
pub mod proxy;
pub mod virtual_instrument;
pub mod vxi11;
pub mod vxi11_fault;

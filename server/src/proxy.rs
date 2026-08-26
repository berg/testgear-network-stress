// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 testgear-network-stress contributors
//
// A fault-injecting TCP proxy: the client connects here, this connects to the
// real server on loopback, and bytes are pumped between the two with the
// transport knobs from `faults.rs` applied on the way back.
//
// Doing it this way — rather than adding injection points inside the servers
// — buys two things.
//
// First, the vendored HiSLIP and VXI-11 code stays byte-identical to ugpibd's,
// so re-vendoring an upstream fix is a copy and not a merge. Test scaffolding
// inside a protocol implementation is scaffolding you then have to reconcile
// forever.
//
// Second, the fault lands where the client actually meets it. A server that
// knows it is misbehaving can be polite about it — flush cleanly, close the
// session, answer the next request as if nothing happened. A proxy that stops
// forwarding mid-record gives the client the thing it must actually survive:
// a socket that went quiet in the middle of a length-prefixed message with no
// explanation.

use std::sync::Arc;

use anyhow::Result;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tracing::debug;

use crate::faults::Faults;
use crate::vxi11_fault::Tracker;

/// Accept on `listener` and proxy every connection to `upstream`.
///
/// `tracker` is present only for the VXI-11 side, where faults are expressed
/// in terms of RPC records rather than bytes. HiSLIP passes None and the
/// bytes are never parsed.
pub async fn run(
    listener: TcpListener,
    upstream: String,
    faults: Arc<Faults>,
    tracker: Option<Arc<Tracker>>,
) -> Result<()> {
    loop {
        let (client, peer) = listener.accept().await?;
        let upstream = upstream.clone();
        let faults = faults.clone();
        let tracker = tracker.clone();
        tokio::spawn(async move {
            if let Err(err) = pump(client, &upstream, faults, tracker).await {
                debug!(%peer, %err, "proxied connection ended");
            }
        });
    }
}

async fn pump(
    client: TcpStream,
    upstream: &str,
    faults: Arc<Faults>,
    tracker: Option<Arc<Tracker>>,
) -> Result<()> {
    let server = TcpStream::connect(upstream).await?;
    // Nagle would coalesce the segments the dribble knob exists to split, and
    // would blur the timing the latency knob exists to control.
    let _ = client.set_nodelay(true);
    let _ = server.set_nodelay(true);

    let (mut client_rx, mut client_tx) = client.into_split();
    let (mut server_rx, mut server_tx) = server.into_split();

    // Client -> server is forwarded verbatim. The faults model what the
    // *instrument side* does to the stream; corrupting what the client sent
    // would test the server, which is not what this suite is for.
    let call_tracker = tracker.clone();
    let to_server = async move {
        let mut buf = vec![0u8; 65536];
        loop {
            let n = client_rx.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            // Note what was asked, so a reply can be matched to a procedure.
            // The request itself is never altered: the faults model what the
            // instrument side does, and corrupting what the client sent would
            // be testing the server.
            if let Some(t) = &call_tracker {
                t.observe_calls(&buf[..n]);
            }
            server_tx.write_all(&buf[..n]).await?;
        }
        // A half-close must propagate, or a client that shuts down its write
        // side while waiting for a final reply deadlocks against a server
        // that is waiting for more request bytes.
        let _ = server_tx.shutdown().await;
        Ok::<_, anyhow::Error>(())
    };

    let to_client = async move {
        let mut buf = vec![0u8; 65536];
        let mut forwarded: u64 = 0;
        loop {
            let n = server_rx.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            let chunk = &buf[..n];

            // Drop: forward up to the byte count, then kill the connection
            // without a FIN handshake, so the client sees a reset rather
            // than a clean end of stream.
            if let Some(limit) = faults.drop_after() {
                if forwarded + chunk.len() as u64 > limit {
                    let allowed = limit.saturating_sub(forwarded) as usize;
                    if allowed > 0 {
                        write_chunk(&mut client_tx, &chunk[..allowed], &faults).await?;
                    }
                    debug!(limit, "injected connection drop");
                    // Dropping the writer without shutdown() sends RST when
                    // there is unread data, which is the abrupt loss a real
                    // instrument reset produces.
                    return Ok::<_, anyhow::Error>(());
                }
            }

            // Stall: forward up to the byte count, then stop reading and
            // stop writing, holding the connection open indefinitely.
            if let Some(limit) = faults.stall_after() {
                if forwarded + chunk.len() as u64 > limit {
                    let allowed = limit.saturating_sub(forwarded) as usize;
                    if allowed > 0 {
                        write_chunk(&mut client_tx, &chunk[..allowed], &faults).await?;
                    }
                    debug!(limit, "injected stall");
                    std::future::pending::<()>().await;
                }
            }

            forwarded += chunk.len() as u64;
            let rewritten = tracker.as_ref().and_then(|t| t.rewrite_replies(chunk));
            match &rewritten {
                Some(body) => write_chunk(&mut client_tx, body, &faults).await?,
                None => write_chunk(&mut client_tx, chunk, &faults).await?,
            }
        }
        let _ = client_tx.shutdown().await;
        Ok::<_, anyhow::Error>(())
    };

    // Either direction finishing ends the connection: a proxy that outlived
    // one half would leave the client waiting on bytes that can no longer
    // arrive, which is a fault this suite injects deliberately and must not
    // suffer by accident.
    tokio::select! {
        r = to_server => r?,
        r = to_client => r?,
    }
    Ok(())
}

/// Write one chunk, honouring the latency and dribble knobs.
async fn write_chunk<W>(w: &mut W, chunk: &[u8], faults: &Faults) -> Result<()>
where
    W: AsyncWriteExt + Unpin,
{
    if let Some(delay) = faults.latency() {
        tokio::time::sleep(delay).await;
    }
    if faults.dribble() {
        for byte in chunk {
            w.write_all(&[*byte]).await?;
            w.flush().await?;
            tokio::time::sleep(std::time::Duration::from_millis(1)).await;
        }
    } else {
        w.write_all(chunk).await?;
    }
    Ok(())
}

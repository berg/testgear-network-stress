"""HiSLIP conformance probe for ugpibd. Read-only apart from status registers."""
import socket, struct, time, threading
import pyvisa
from pyvisa import constants
from pyvisa_py.protocols import hislip

HOST, PORT = "127.0.0.1", 4880
RN = f"TCPIP0::{HOST}::hislip23,{PORT}::INSTR"
rm = pyvisa.ResourceManager("@py")
def opn():
    i = rm.open_resource(RN, open_timeout=5000); i.timeout = 8000; return i
def iface(i): return i.visalib.sessions[i.session].interface

print("=== 1. unknown sub-address ===")
for sub in ("hislip0", "hislip99", "bogus"):
    t0 = time.time()
    try:
        i = rm.open_resource(f"TCPIP0::{HOST}::{sub},{PORT}::INSTR", open_timeout=3000)
        print(f"  {sub:9s} opened (unexpected)"); i.close()
    except Exception as e:
        print(f"  {sub:9s} {type(e).__name__} after {time.time()-t0:.1f}s: {str(e)[:52]}")

print("\n=== 2. AsyncLockInfo / lock semantics ===")
a, b = opn(), opn()
ia, ib = iface(a), iface(b)
print(f"  lock_info with no lock held:        {ia.async_lock_info()}  (expect 0)")
print(f"  A exclusive lock:                   {ia.async_lock_request(2.0, '')!r}")
print(f"  lock_info while A holds exclusive:  {ia.async_lock_info()}  (expect 1)")
print(f"  B exclusive lock while A holds it:  {ib.async_lock_request(1.0, '')!r}  (expect 'failure')")
print(f"  A release:                          {ia.async_lock_release()!r}")
print(f"  B release (holds nothing):          {ib.async_lock_release()!r}  (expect 'failure'/'error')")
print(f"  A shared 'k1':                      {ia.async_lock_request(2.0, 'k1')!r}")
print(f"  B shared 'k2' (different name):     {ib.async_lock_request(1.0, 'k2')!r}  (expect 'failure')")
print(f"  B shared 'k1' (same name):          {ib.async_lock_request(1.0, 'k1')!r}  (expect success shared)")
for x in (ia, ib):
    try: x.async_lock_release()
    except Exception: pass

print("\n=== 3. max message size negotiation ===")
print(f"  server max (asked for 1MB):  {ia.async_maximum_message_size(1<<20)}")
print(f"  server max (asked for 4kB):  {ia.async_maximum_message_size(4096)}")
ia.max_msg_size = 1<<20

print("\n=== 4. Data message id echo ===")
a.write("*IDN?")
hdr = hislip.RxHeader(ia._sync)
print(f"  client last sent id={ia.last_message_id:#x}; server replied "
      f"{hdr.msg_type} with id={hdr.message_parameter:#x} "
      f"(expect that id or 0xffffffff)")
hislip.receive_flush(ia._sync, hdr.payload_length)
ia._msg_type, ia._payload_remaining = "", 0

print("\n=== 5. Interrupted on device clear with a read outstanding ===")
a.write("*CLS")                      # no response -> a read would block
feature = ia.async_device_clear()
ia._sync.settimeout(2.0)
try:
    h = hislip.RxHeader(ia._sync)
    print(f"  server sent {h.msg_type} after AsyncDeviceClear")
except Exception as e:
    print(f"  no Interrupted within 2s ({type(e).__name__}) -- spec says the server "
          f"should send one for the discarded message")
ia._sync.settimeout(8.0)
ia.device_clear_complete(feature); ia._reset_message_state()

print("\n=== 6. SRQ: MAV vs ESB path ===")
a.enable_event(constants.EventType.service_request, constants.EventMechanism.queue)
for label, setup in (("MAV (*SRE 16)", ["*CLS","*SRE 16","*IDN?"]),
                     ("ESB (*SRE 32/*ESE 1)", ["*CLS","*ESE 1","*SRE 32","*OPC"])):
    for c in setup: a.write(c)
    r = a.wait_on_event(constants.EventType.service_request, 4000, capture_timeout=True)
    print(f"  {label:24s} {'fired' if not r.timed_out else 'NO SRQ within 4s'}")
    try: a.read_raw()
    except Exception: pass
a.write("*CLS"); a.write("*SRE 0"); a.write("*ESE 0")

print("\n=== 7. unrecognized message type ===")
sock = ia._sync
sock.sendall(struct.pack(hislip.HEADER_FORMAT, b"HS", 120, 0, 0, 0))  # reserved type
sock.settimeout(2.0)
try:
    h = hislip.RxHeader(sock)
    print(f"  server replied {h.msg_type} (spec: Error 'Unrecognized Message Type')")
except Exception as e:
    print(f"  no reply within 2s ({type(e).__name__}) -- spec wants an Error message")
for x in (a, b):
    try: x.close()
    except Exception: pass

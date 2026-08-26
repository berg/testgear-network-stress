"""Separate real defects from artifacts of how the earlier tests were written."""
import select, time
import pyvisa
from pyvisa import constants
from pyvisa_py.protocols import hislip
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")
a = rm.open_resource(RN, open_timeout=5000); a.timeout = 8000
ia = a.visalib.sessions[a.session].interface
sock = ia._sync
a.query("*IDN?")

def buffered():
    r, _, _ = select.select([sock], [], [], 0)
    return bool(r)

print("=== P3a: was the response ALREADY delivered before the device clear? ===")
a.write("*IDN?")
time.sleep(0.6)                      # let the push happen
print(f"  sync socket readable before AsyncDeviceClear: {buffered()}")
feature = ia.async_device_clear()
print(f"  -> if True, the DataEND I saw earlier was already on the wire;")
print(f"     the spec makes the CLIENT discard it, so ugpibd did nothing wrong")
sock.settimeout(2.0)
try:
    h = hislip.RxHeader(sock)
    if h.payload_length: hislip.receive_flush(sock, h.payload_length)
    print(f"  message after the clear ack: {h.msg_type}")
except Exception as e:
    print(f"  nothing after the clear ack ({type(e).__name__})")
sock.settimeout(8.0)
ia.device_clear_complete(feature); ia._reset_message_state()
for _ in range(8):
    if a.query("SYST:ERR?").strip().startswith(("+0,","0,")): break

print("\n=== P3b: device clear while the reply is genuinely still in flight ===")
a.write("*IDN?")
print(f"  sync socket readable immediately after write: {buffered()}  (want False)")
feature = ia.async_device_clear()    # issued while ugpibd is still on the bus
sock.settimeout(2.5)
seen = []
try:
    while True:
        h = hislip.RxHeader(sock)
        if h.payload_length: hislip.receive_flush(sock, h.payload_length)
        seen.append(h.msg_type)
        if h.msg_type in ("Interrupted", "DataEnd"): break
except Exception as e:
    seen.append(f"<{type(e).__name__}>")
print(f"  server sent: {seen}")
print("  Interrupted = conformant; DataEnd = reply delivered despite the clear")
sock.settimeout(8.0)
try:
    ia.device_clear_complete(feature)
except Exception as e:
    print(f"  (clear handshake: {type(e).__name__})")
ia._reset_message_state()
for _ in range(8):
    if a.query("SYST:ERR?").strip().startswith(("+0,","0,")): break

print("\n=== P2: is the SRQ line watched by interrupt or by polling? ===")
a.enable_event(constants.EventType.service_request, constants.EventMechanism.queue)
lat = []
for _ in range(8):
    a.write("*CLS"); a.write("*ESE 1"); a.write("*SRE 32")
    t0 = time.time(); a.write("*OPC")
    r = a.wait_on_event(constants.EventType.service_request, 4000, capture_timeout=True)
    if not r.timed_out: lat.append((time.time()-t0)*1000)
if lat:
    print(f"  ESB SRQ latency ms: min={min(lat):.0f} max={max(lat):.0f} "
          f"mean={sum(lat)/len(lat):.0f}  n={len(lat)}")
    print("  a tight spread of tens-of-ms suggests polling; sub-ms suggests interrupts")
a.write("*CLS"); a.write("*SRE 0"); a.write("*ESE 0")
a.close()

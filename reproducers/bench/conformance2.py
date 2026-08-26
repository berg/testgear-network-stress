import time
import pyvisa
from pyvisa import constants
from pyvisa_py.protocols import hislip
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")
a = rm.open_resource(RN, open_timeout=5000); a.timeout = 8000
ia = a.visalib.sessions[a.session].interface

print("=== Interrupted, with a response genuinely pending ===")
a.write("*IDN?")               # response now queued in the instrument
time.sleep(0.3)
feature = ia.async_device_clear()   # server should discard it and say Interrupted
ia._sync.settimeout(2.5)
try:
    h = hislip.RxHeader(ia._sync)
    print(f"  server sent {h.msg_type}")
    if h.payload_length: hislip.receive_flush(ia._sync, h.payload_length)
except Exception as e:
    print(f"  nothing within 2.5s ({type(e).__name__})")
ia._sync.settimeout(8.0)
ia.device_clear_complete(feature); ia._reset_message_state()
for _ in range(10):
    if a.query("SYST:ERR?").strip().startswith(("+0,","0,")): break

print("\n=== MAV SRQ: did the instrument assert RQS, or did ugpibd drop it? ===")
a.enable_event(constants.EventType.service_request, constants.EventMechanism.queue)
a.write("*CLS"); a.write("*SRE 16")
a.write("*IDN?")                    # queues output -> MAV -> should assert SRQ
time.sleep(0.5)
stb = a.read_stb()
r = a.wait_on_event(constants.EventType.service_request, 3000, capture_timeout=True)
print(f"  status byte = {stb:#04x}  (MAV=0x10, RQS=0x40)")
print(f"  RQS asserted by instrument: {bool(stb & 0x40)}")
print(f"  AsyncServiceRequest delivered: {not r.timed_out}")
if stb & 0x40 and r.timed_out:
    print("  -> instrument DID request service; ugpibd did not forward it")
elif not stb & 0x40:
    print("  -> instrument never asserted RQS; not a gateway fault")
try: a.read_raw()
except Exception: pass

print("\n=== ESB SRQ for comparison ===")
a.write("*CLS"); a.write("*ESE 1"); a.write("*SRE 32"); a.write("*OPC")
time.sleep(0.5)
stb2 = a.read_stb()
r2 = a.wait_on_event(constants.EventType.service_request, 3000, capture_timeout=True)
print(f"  status byte = {stb2:#04x}; RQS={bool(stb2 & 0x40)}; delivered={not r2.timed_out}")
a.write("*CLS"); a.write("*SRE 0"); a.write("*ESE 0")
a.close()

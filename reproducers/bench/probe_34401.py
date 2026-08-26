import time
import pyvisa
from pyvisa import constants
from pyvisa.constants import ResourceAttribute as RA
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")
inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 5000
lib, sess = inst.visalib, inst.session

def show(label, fn):
    try: print(f"{label:32s} {fn()}")
    except Exception as e: print(f"{label:32s} !! {type(e).__name__}: {str(e)[:60]}")

show("*IDN?", lambda: inst.query("*IDN?").strip())
show("SYST:ERR?", lambda: inst.query("SYST:ERR?").strip())

print("\n--- speed ---")
t0=time.time(); n=30
for _ in range(n): inst.query("*IDN?")
dt=time.time()-t0
print(f"{'query rate':32s} {n/dt:.0f}/s ({1000*dt/n:.1f} ms each)")

print("\n--- large-response candidates ---")
for q in ("*LRN?","SYST:VERS?","*OPT?","CALC:FUNC?","SENS:FUNC?"):
    def f(q=q):
        r = inst.query(q); return f"{len(r)}B {r[:50]!r}"
    show(q, f)

print("\n--- operations ---")
show("read_stb", lambda: hex(inst.read_stb()))
show("assert_trigger", lambda: lib.assert_trigger(sess, constants.TriggerProtocol.default))
show("  err after trigger", lambda: inst.query("SYST:ERR?").strip())
show("clear", lambda: lib.clear(sess))
show("lock exclusive", lambda: lib.lock(sess, constants.Lock.exclusive, 2000, None))
show("unlock", lambda: lib.unlock(sess))
show("lock shared", lambda: lib.lock(sess, constants.Lock.shared, 2000, "k"))
show("unlock", lambda: lib.unlock(sess))
for m in constants.RENLineOperation:
    show(f"REN {m.name}", lambda m=m: lib.gpib_control_ren(sess, m))
show("  err after REN", lambda: inst.query("SYST:ERR?").strip())

print("\n--- SRQ ---")
try:
    inst.enable_event(constants.EventType.service_request, constants.EventMechanism.queue)
    inst.write("*CLS"); inst.write("*SRE 16"); inst.write("*IDN?")
    inst.wait_on_event(constants.EventType.service_request, 5000)
    print(f"{'MAV SRQ':32s} fired; stb={hex(inst.read_stb())}; drained={inst.read_raw()[:30]!r}")
except Exception as e:
    print(f"{'MAV SRQ':32s} !! {type(e).__name__}: {str(e)[:70]}")
try:
    inst.write("*CLS"); inst.write("*ESE 1"); inst.write("*SRE 32"); inst.write("*OPC")
    inst.wait_on_event(constants.EventType.service_request, 5000)
    print(f"{'OPC SRQ':32s} fired; stb={hex(inst.read_stb())}")
except Exception as e:
    print(f"{'OPC SRQ':32s} !! {type(e).__name__}: {str(e)[:70]}")
inst.write("*CLS"); inst.write("*SRE 0"); inst.write("*ESE 0")
show("final SYST:ERR?", lambda: inst.query("SYST:ERR?").strip())
show("max_msg_kb", lambda: inst.get_visa_attribute(RA.tcpip_hislip_max_message_kb))
inst.close()

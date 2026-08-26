import time, threading
import pyvisa
from pyvisa import constants
from pyvisa.constants import ResourceAttribute as RA

RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
rm = pyvisa.ResourceManager("@py")
inst = rm.open_resource(RN, open_timeout=5000)
inst.timeout = 5000
lib, sess = inst.visalib, inst.session

def show(label, fn):
    try:
        print(f"{label:38s} {fn()}")
    except Exception as e:
        print(f"{label:38s} !! {type(e).__name__}: {e}")

print("=== identity / errors ===")
show("*IDN?", lambda: inst.query("*IDN?").strip())
show("SYST:ERR?", lambda: inst.query("SYST:ERR?").strip())

print("\n=== large response candidates ===")
for q in ("SYST:HELP:HEAD?", "*OPT?", "*LRN?", "SYST:VERS?"):
    def f(q=q):
        r = inst.query(q)
        return f"{len(r)} bytes: {r[:60]!r}"
    show(q, f)

print("\n=== operations ===")
show("read_stb", lambda: hex(inst.read_stb()))
show("assert_trigger", lambda: lib.assert_trigger(sess, constants.TriggerProtocol.default))
show("clear", lambda: lib.clear(sess))
show("lock exclusive", lambda: lib.lock(sess, constants.Lock.exclusive, 2000, None))
show("unlock", lambda: lib.unlock(sess))
show("lock shared", lambda: lib.lock(sess, constants.Lock.shared, 2000, "stress"))
show("unlock", lambda: lib.unlock(sess))
show("REN assert", lambda: lib.gpib_control_ren(sess, constants.RENLineOperation.asrt))
show("REN deassert_gtl", lambda: lib.gpib_control_ren(sess, constants.RENLineOperation.deassert_gtl))

print("\n=== SRQ ===")
try:
    inst.enable_event(constants.EventType.service_request, constants.EventMechanism.queue)
    inst.write("*CLS")
    inst.write("*SRE 16")          # MAV
    inst.write("*IDN?")            # queues output -> MAV -> SRQ
    ev = inst.wait_on_event(constants.EventType.service_request, 4000)
    print(f"{'MAV SRQ (*SRE 16)':38s} fired, stb={hex(inst.read_stb())}")
    print(f"{'  drained':38s} {inst.read_raw()[:40]!r}")
except Exception as e:
    print(f"{'MAV SRQ (*SRE 16)':38s} !! {type(e).__name__}: {e}")

try:
    inst.write("*CLS"); inst.write("*SRE 32"); inst.write("*ESE 1")
    inst.write("*OPC")
    ev = inst.wait_on_event(constants.EventType.service_request, 4000)
    print(f"{'OPC SRQ (*SRE 32/*ESE 1)':38s} fired, stb={hex(inst.read_stb())}")
except Exception as e:
    print(f"{'OPC SRQ (*SRE 32/*ESE 1)':38s} !! {type(e).__name__}: {e}")
inst.write("*CLS"); inst.write("*SRE 0")

print("\n=== attributes ===")
for name in ("tcpip_hislip_max_message_kb","tcpip_hislip_version","tcpip_hislip_overlap_enable",
             "tcpip_keepalive","resource_lock_state","interface_instrument_name"):
    show(name, lambda n=name: inst.get_visa_attribute(getattr(RA, n)))

inst.close(); rm.close()

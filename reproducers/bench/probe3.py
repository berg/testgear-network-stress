import pyvisa
from pyvisa.constants import ResourceAttribute as RA
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
rm = pyvisa.ResourceManager("@py"); inst = rm.open_resource(RN, open_timeout=5000)
inst.timeout = 10000

# compound query chains: long write + long read, content verifiable
for n in (2, 10, 50, 200):
    cmd = ";".join(["*IDN?"] * n)
    try:
        r = inst.query(cmd).strip()
        parts = r.split(";")
        print(f"chain n={n:4d} write={len(cmd):6d}B read={len(r):6d}B parts={len(parts)} ok={len(parts)==n}")
    except Exception as e:
        print(f"chain n={n:4d} !! {type(e).__name__}: {e}")
        try: inst.clear()
        except Exception: pass

# forced chunking: shrink max message size, then do a big read
print("\nmax_msg_kb default:", inst.get_visa_attribute(RA.tcpip_hislip_max_message_kb))
for kb in (1, 4):
    inst.set_visa_attribute(RA.tcpip_hislip_max_message_kb, kb)
    got = inst.get_visa_attribute(RA.tcpip_hislip_max_message_kb)
    r = inst.query("*LRN?")
    print(f"  set {kb}kb -> {got}kb, *LRN? = {len(r)} bytes, head={r[:12]!r}")
inst.set_visa_attribute(RA.tcpip_hislip_max_message_kb, 1024)
print("SYST:ERR? ->", inst.query("SYST:ERR?").strip())
inst.close(); rm.close()

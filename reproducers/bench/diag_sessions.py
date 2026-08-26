"""How many concurrent HiSLIP sessions does this instrument allow?"""
import pyvisa, traceback
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
rms, insts = [], []
for i in range(8):
    try:
        rm = pyvisa.ResourceManager("@py")
        inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 3000
        idn = inst.query("*IDN?").strip()
        rms.append(rm); insts.append(inst)
        print(f"session {i}: OK  ({idn[:40]})")
    except Exception as e:
        print(f"session {i}: FAIL {type(e).__name__}: {e}")
        break
print(f"\n-> {len(insts)} concurrent sessions succeeded")
# verify the surviving ones still work
for i, inst in enumerate(insts):
    try:
        print(f"  session {i} still alive: {inst.query('*IDN?').strip()[:30]}")
    except Exception as e:
        print(f"  session {i} broken: {type(e).__name__}: {e}")
for inst in insts: 
    try: inst.close()
    except Exception: pass
for rm in rms:
    try: rm.close()
    except Exception: pass

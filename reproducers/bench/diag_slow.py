import time
import pyvisa
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")
inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 20000

def rate(n=10):
    t0=time.time()
    for _ in range(n): inst.query("*IDN?")
    return n/(time.time()-t0)

print(f"current rate: {rate():.1f} queries/s")
print("\ninstrument configuration (read-only):")
for q in ("TRIG:SOUR?","TRIG:COUN?","SAMP:COUN?","SENS:FUNC?","SENS:VOLT:DC:NPLC?",
          "SENS:VOLT:DC:RANG:AUTO?","INP:IMP:AUTO?","CALC:STAT?","SYST:ERR?"):
    try: print(f"  {q:26s} {inst.query(q).strip()!r}")
    except Exception as e: print(f"  {q:26s} !! {type(e).__name__}")

print("\nper-query latency samples (ms):")
for _ in range(6):
    t0=time.time(); inst.query("*IDN?"); print(f"  {1000*(time.time()-t0):.0f}", end="")
print()
print("\nlatency of a non-query write (*CLS):")
for _ in range(3):
    t0=time.time(); inst.write("*CLS"); print(f"  {1000*(time.time()-t0):.1f}", end="")
print()
print(f"\nrate after a device clear: ", end="")
inst.clear(); print(f"{rate():.1f} queries/s")
inst.close()

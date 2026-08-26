"""MAV (STB bit 4) must reflect message-available regardless of the SRE mask.
IEEE 488.2: SRE gates RQS/SRQ only; MAV is set whenever output is queued."""
import sys, time
import pyvisa
RN = sys.argv[1]
rm = pyvisa.ResourceManager("@py")
i = rm.open_resource(RN, open_timeout=8000); i.timeout = 8000
i.query("*IDN?")
for _ in range(10):
    if i.query("SYST:ERR?").strip().startswith(("+0,","0,")): break

for sre in (0, 16):
    i.write("*CLS"); i.write(f"*SRE {sre}")
    i.write("*IDN?")                 # queue a reply, leave it unread
    time.sleep(0.4)
    stb = i.read_stb()
    print(f"  *SRE {sre:<2d}: stb={stb:#04x}  MAV={'set' if stb & 0x10 else 'CLEAR'}"
          f"  RQS={'set' if stb & 0x40 else 'clear'}")
    try: i.read_raw()
    except Exception: pass
i.write("*CLS"); i.write("*SRE 0")
i.close()

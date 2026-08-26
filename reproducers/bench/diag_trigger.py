"""Minimal reproducer: which trigger sequence resets the connection?"""
import sys, time
import pyvisa
from pyvisa import constants
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"

def run(label, body, n=60):
    rm = pyvisa.ResourceManager("@py")
    inst = rm.open_resource(RN, open_timeout=10000); inst.timeout = 5000
    lib, sess = inst.visalib, inst.session
    inst.query("*IDN?")
    try:
        for i in range(n):
            body(inst, lib, sess, i)
        print(f"  {label:34s} survived {n}")
    except Exception as e:
        print(f"  {label:34s} FAILED at {i}: {type(e).__name__}: {str(e)[:55]}")
    finally:
        try: inst.close()
        except Exception: pass

run("trigger only", lambda inst,lib,sess,i: lib.assert_trigger(sess, constants.TriggerProtocol.default))
run("trigger + query", lambda inst,lib,sess,i: (lib.assert_trigger(sess, constants.TriggerProtocol.default), inst.query("*IDN?")))
run("trigger + clear", lambda inst,lib,sess,i: (lib.assert_trigger(sess, constants.TriggerProtocol.default), lib.clear(sess)))
run("clear only", lambda inst,lib,sess,i: lib.clear(sess))
run("trigger x3 then clear", lambda inst,lib,sess,i: ([lib.assert_trigger(sess, constants.TriggerProtocol.default) for _ in range(3)], lib.clear(sess)))

# verify_timebase.sh - external AWG staircase: sweep AWG TRIG:DEL tau, measure
# output->input interval M (cmd 0x42, eCAP timestamp), fit M vs tau. PASS = slope
# ~1.000 (PRU tick = 5.000 ns vs the Keysight crystal). Needs: out0(P8_11)->AWG
# ext-trig, AWG out->P8_16, common GND, AWG on LAN at AWG_IP.
cd ~/pru/timing_verify || exit 1
AWG_IP=192.168.0.7 TAU_STOP=1000 TAU_STEP=4 N_SAMP=40 REARM_S=0.005 python3 run_awg_bbb.py
echo "#### DONE ####"

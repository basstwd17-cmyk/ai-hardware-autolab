/*
 * bbb_pulse_io  -  PRU0 firmware reproducing the BeagleY-AI pulse-I/O on the
 * BeagleBone Black (AM335x) PRU-ICSS at 200 MHz (1 instruction = 1 cycle = 5 ns).
 *
 * Control channel : RPMsg (rpmsg-raw, /dev/rpmsgN), 1-byte command protocol,
 *                   mirroring the BeagleY-AI R5F firmware.
 * Output          : PRU0 R30 bit 15 -> header pin P8_11 (pinmux mode 6, set by
 *                   the Linux host via /dev/mem before loading).  Optional out1
 *                   = R30 bit 14 -> P8_12.
 * Timing          : every edge is placed with __delay_cycles(), a compile-time
 *                   constant => cycle-exact (5 ns granularity, 0 jitter within a
 *                   burst).  This is the BBB analogue of the BeagleY-AI 8 ns ECAP
 *                   burst, now at 5 ns.
 *
 * Commands (payload[0] = cmd):
 *   0x01 PING                         -> [0x01][0x01]
 *   0x03 VERSION                      -> [0x03][ascii...]
 *   0x10 [reps:4]   R1 pattern        off500/on2000/off600/on500/off500 ns,
 *                                     reps times (reps==0 => forever; stop via
 *                                     remoteproc).  reply [0x10][1][reps:4]
 *   0x20 [reps:4]   ≈ 200 ns pulse, ≈50% duty (40c high / 40c low — __delay_cycles
 *                                     does not absorb edge/loop overhead), reps (0=forever)
 *   0x21 [reps:4]   ≈ 100 ns / ≈ 100 ns square (20c/20c — ≈50% duty, __delay_cycles
 *                                     does not absorb edge/loop overhead), reps (0=forever)
 *   0x22 [reps:4]   minimum-width demo: ~10 ns high (two instruction cycles) every
 *                                     500 ns, reps
 */
#include <stdint.h>
#include <pru_cfg.h>
#include <pru_intc.h>
#include <pru_ecap.h>
#include <rsc_types.h>
#include <pru_rpmsg.h>
#include "resource_table.h"
#include "intc_map_0.h"

volatile register uint32_t __R30;   /* PRU GPO  */
volatile register uint32_t __R31;   /* PRU GPI / host-int */

#define HOST_INT                 ((uint32_t)1 << 30)
#define TO_ARM_HOST              16
#define FROM_ARM_HOST            17
#define CHAN_NAME                "rpmsg-raw"
#define CHAN_PORT                30
#define VIRTIO_CONFIG_S_DRIVER_OK 4

#define OUT0   (1u << 15)   /* R30.15 -> P8_11  (output) */
#define OUT1   (1u << 14)   /* R30.14 -> P8_12  (output, if muxed) */
#define RTASE_UPDATE (1u << 0) /* R30.0 -> P9_31 -> RTASE LDAC.A */
#define RTASE_OUTPUTS (OUT0 | OUT1 | RTASE_UPDATE)
#define IN0    (1u << 14)   /* R31.14 -> P8_16  (loopback input) */
/* PRU LOOP uses only the low 16 bits of a register count.  Compose 50 ms from
 * 152 * 65,535 + 38,680 = 10,000,000 NOP cycles instead of passing 10,000,000
 * directly to pru_delay(). */
#define PRU_DELAY_MAX_CYCLES          65535u
#define HALF_PERIOD_10HZ_FULL_BLOCKS    152u
#define HALF_PERIOD_10HZ_REMAINDER    38680u

extern void pru_delay(uint32_t n);   /* pru_delay.asm: exact (n & 0xffff)-cycle HW-LOOP delay */

uint8_t payload[RPMSG_MESSAGE_SIZE];
static struct pru_rpmsg_transport transport;
static uint16_t src, dst, len;

/* 10,000 preloaded two-bit values occupy 2,500 bytes of PRU0's 8 KiB local
 * RAM.  The Linux/SSH path fills this before a run; no Linux or network access
 * occurs in the timed loop. */
#define RTASE_MAX_SYMBOLS 10000u
#define RTASE_PACKED_BYTES (RTASE_MAX_SYMBOLS / 4u)
static uint8_t rtase_packed[RTASE_PACKED_BYTES];
static uint16_t rtase_symbol_count;

/* one R1 pattern cycle: 500/2000/600/500/500 ns @ 5 ns/cycle = 100/400/120/100/100 */
static void pattern_once(void)
{
    __R30 &= ~OUT0; __delay_cycles(100);   /* off 500 ns  */
    __R30 |=  OUT0; __delay_cycles(400);   /* on  2000 ns */
    __R30 &= ~OUT0; __delay_cycles(120);   /* off 600 ns  */
    __R30 |=  OUT0; __delay_cycles(100);   /* on  500 ns  */
    __R30 &= ~OUT0; __delay_cycles(100);   /* off 500 ns  */
}

static uint32_t rd32(const uint8_t *p)
{ return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24); }
static void wr32(uint8_t *p, uint32_t v)
{ p[0]=(uint8_t)v; p[1]=(uint8_t)(v>>8); p[2]=(uint8_t)(v>>16); p[3]=(uint8_t)(v>>24); }
static uint16_t rd16(const uint8_t *p)
{ return (uint16_t)p[0] | ((uint16_t)p[1] << 8); }
static void wr16(uint8_t *p, uint16_t v)
{ p[0]=(uint8_t)v; p[1]=(uint8_t)(v>>8); }

static uint8_t rtase_symbol(uint16_t index)
{
    return (rtase_packed[index >> 2] >> ((index & 3u) << 1)) & 3u;
}

static void rtase_set_symbol(uint16_t index, uint8_t value)
{
    uint16_t byte_index = index >> 2;
    uint8_t shift = (uint8_t)((index & 3u) << 1);
    uint8_t mask = (uint8_t)(3u << shift);
    rtase_packed[byte_index] =
        (uint8_t)((rtase_packed[byte_index] & (uint8_t)~mask) |
                  ((value & 3u) << shift));
}

/* Long inter-shot delays are not in the critical selection-to-update path.
 * Split them into the 16-bit chunks supported by the PRU hardware LOOP. */
static void rtase_delay_long(uint32_t cycles)
{
    while (cycles > PRU_DELAY_MAX_CYCLES) {
        pru_delay(PRU_DELAY_MAX_CYCLES);
        cycles -= PRU_DELAY_MAX_CYCLES;
    }
    pru_delay(cycles);
}

/* send payload[0..n-1] back to the host on the current RPMsg channel */
static void reply(uint16_t n) { pru_rpmsg_send(&transport, dst, src, payload, n); }

/* NAK ([cmd][0xEF]) and early-out the switch case when the request is too short.
 * A macro, not a function: the `break` must exit the enclosing switch case.
 * (payload[0] already holds cmd from pru_rpmsg_receive.) */
#define REQUIRE_LEN(n) if (len < (n)) { payload[1] = 0xEF; reply(2); break; }

void main(void)
{
    volatile uint8_t *status;

    CT_CFG.SYSCFG_bit.STANDBY_INIT = 0;        /* enable OCP master port */
    __R30 &= ~RTASE_OUTPUTS;                     /* idle low */
    /* remoteproc does not guarantee that PRU data RAM is cleared between
     * stop/start cycles.  Never expose a stale sequence after a restart. */
    rtase_symbol_count = 0;

    /* Free-run the PRU-ICSS eCAP TSCTR (200 MHz = 5 ns) as the timestamp source.
     * CT_ECAP is a "near" peripheral so reads are ~2 cycles (vs IEP "far" ~14 cyc).
     * ECCTL2 TSCTRSTOP(bit4)=1 makes TSCTR free-run. Used by cmd 0x42/0x43. */
    CT_ECAP.ECCTL2 = 0x0010;

    CT_INTC.SICR_bit.STS_CLR_IDX = FROM_ARM_HOST;
    status = &resourceTable.rpmsg_vdev.status;
    while (!(*status & VIRTIO_CONFIG_S_DRIVER_OK));

    pru_rpmsg_init(&transport, &resourceTable.rpmsg_vring0,
                   &resourceTable.rpmsg_vring1, TO_ARM_HOST, FROM_ARM_HOST);
    while (pru_rpmsg_channel(RPMSG_NS_CREATE, &transport, CHAN_NAME, CHAN_PORT)
           != PRU_RPMSG_SUCCESS);

    while (1) {
        if (__R31 & HOST_INT) {
            CT_INTC.SICR_bit.STS_CLR_IDX = FROM_ARM_HOST;
            while (pru_rpmsg_receive(&transport, &src, &dst, payload, &len)
                   == PRU_RPMSG_SUCCESS) {
                uint8_t cmd = payload[0];
                uint32_t reps, i;

                switch (cmd) {
                case 0x01:                                  /* PING */
                    payload[0] = 0x01;
                    payload[1] = 0x01;
                    reply(2);
                    break;

                case 0x03: {                                /* VERSION */
                    const char *v = "bbb_pulse_io v1.9-rtase pru0 sel=P8_11/P8_12 update=P9_31 + random-buffer 0x50/0x51 + run 0x52";
                    int k = 0;
                    payload[0] = 0x03;
                    while (v[k]) { payload[1+k] = (uint8_t)v[k]; k++; }
                    reply(k+1);
                    break;
                }

                case 0x10:                                  /* R1 pattern */
                    REQUIRE_LEN(5);
                    reps = rd32(&payload[1]);
                    if (reps == 0) { for (;;) pattern_once(); }   /* never returns */
                    for (i = 0; i < reps; i++) pattern_once();
                    __R30 &= ~OUT0;
                    payload[0]=0x10; payload[1]=1; wr32(&payload[2], reps);
                    reply(6);
                    break;

                case 0x20:                                  /* ≈ 200 ns / ≈ 200 ns, ≈50% duty (__delay_cycles does not absorb edge/loop overhead) */
                    REQUIRE_LEN(5);
                    reps = rd32(&payload[1]);
                    if (reps == 0) { for (;;){ __R30|=OUT0; __delay_cycles(40);
                                               __R30&=~OUT0; __delay_cycles(40);} }
                    for (i=0;i<reps;i++){ __R30|=OUT0; __delay_cycles(40);
                                          __R30&=~OUT0; __delay_cycles(40); }
                    __R30 &= ~OUT0;
                    payload[0]=0x20; payload[1]=1;
                    reply(2);
                    break;

                case 0x21:                                  /* ≈ 100 ns / ≈ 100 ns ≈ 5 MHz, ≈50% duty (__delay_cycles does not absorb edge/loop overhead) */
                    REQUIRE_LEN(5);
                    reps = rd32(&payload[1]);
                    if (reps == 0) { for (;;){ __R30|=OUT0; __delay_cycles(20);
                                               __R30&=~OUT0; __delay_cycles(20);} }
                    for (i=0;i<reps;i++){ __R30|=OUT0; __delay_cycles(20);
                                          __R30&=~OUT0; __delay_cycles(20); }
                    __R30 &= ~OUT0;
                    payload[0]=0x21; payload[1]=1;
                    reply(2);
                    break;

                case 0x22:                                  /* ~10 ns high (two instruction cycles) every 500 ns */
                    REQUIRE_LEN(5);
                    reps = rd32(&payload[1]);
                    if (reps == 0) reps = 1000000u;
                    for (i=0;i<reps;i++){ __R30|=OUT0; __R30&=~OUT0; __delay_cycles(99); }
                    payload[0]=0x22; payload[1]=1;
                    reply(2);
                    break;

                case 0x42: {                                /* AWG-SHOT: fire trigger, capture echo (R5/G5) */
                    /* External AWG staircase: out0(P8_11)->AWG ext-trig; AWG emits an echo after
                     * TRIG:DEL=tau into the board input (P8_16/R31.14). Each shot: wait input low,
                     * timestamp T_out (fast eCAP TSCTR) at the trigger edge, poll input for the
                     * echo rising edge, timestamp T_in. M = T_in - T_out (5 ns ticks) = AWG
                     * latency + tau. Long settle so the AWG re-arms. REQ:[42][n:4] REPLY 22B
                     * [42][ok][ngood:4][ntmo:4][min:4][max:4][sum:4]; host: M_ns=mean*5, fit vs tau. */
                    uint32_t n, t0, t1, guard, tmo, g2;
                    uint32_t rmin = 0xFFFFFFFFu, rmax = 0, rsum = 0, ngood = 0, ntmo = 0;
                    REQUIRE_LEN(5);
                    n = rd32(&payload[1]);
                    if (n == 0 || n > 2000) n = 100;
                    for (i = 0; i < n; i++) {
                        __R30 &= ~OUT0;
                        __delay_cycles(2000);          /* ~10 us: low + AWG re-arm */
                        g2 = 0;                        /* make sure input (prev echo) is low first */
                        while ((__R31 & IN0)) { if (++g2 >= 60000u) break; }
                        t0 = CT_ECAP.TSCTR;            /* fast 5 ns counter at trigger instant */
                        __R30 |= OUT0;                 /* rising edge -> AWG ext trig */
                        /* TIGHT echo poll: sample R31 ~every cycle (8x unrolled) so the input
                         * edge is caught within ~1 cycle -> M resolves to ~5 ns, not the ~15 ns
                         * of a guard-in-the-hot-path loop. guard amortized 1/8 for the timeout. */
                        tmo = 1;
                        for (guard = 0; guard < 16000u; guard++) {
                            if (__R31 & IN0) { tmo = 0; break; }
                            if (__R31 & IN0) { tmo = 0; break; }
                            if (__R31 & IN0) { tmo = 0; break; }
                            if (__R31 & IN0) { tmo = 0; break; }
                        }
                        t1 = CT_ECAP.TSCTR;
                        __R30 &= ~OUT0;
                        if (tmo) { ntmo++; continue; }
                        { uint32_t d = t1 - t0;
                          if (d < rmin) rmin = d;
                          if (d > rmax) rmax = d;
                          rsum += d; ngood++; }
                    }
                    payload[0]=0x42; payload[1]=(ngood>0)?1:0;
                    wr32(&payload[2],  ngood);
                    wr32(&payload[6],  ntmo);
                    wr32(&payload[10], (rmin==0xFFFFFFFFu)?0:rmin);
                    wr32(&payload[14], rmax);
                    wr32(&payload[18], rsum);
                    reply(22);
                    break;
                }

                case 0x30:                                  /* 5 ns STAIRCASE FRAME (scope demo) */
                    /* Repeating frame on P8_11: a ~1 us trigger marker, then 10 pulses whose
                     * HIGH width grows by exactly 1 cycle (5 ns) each (because each adds one
                     * more __delay_cycles(1) = 1 PRU cycle = 5 ns). Trigger the scope on the
                     * wide marker; the 10 trailing pulses step by 5 ns. Runs forever
                     * (stop via remoteproc). The 5 ns STEP is exact regardless of the base. */
                    for (;;) {
                        __R30 |= OUT0; __delay_cycles(200); __R30 &= ~OUT0; /* ~1us marker */
                        __delay_cycles(300);
                        __R30 |= OUT0;                    __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(1); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(2); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(3); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(4); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(5); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(6); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(7); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(8); __R30 &= ~OUT0; __delay_cycles(40);
                        __R30 |= OUT0; __delay_cycles(9); __R30 &= ~OUT0; __delay_cycles(40);
                        __delay_cycles(1000);            /* frame separator */
                    }
                    break;

                case 0x31: {                                /* INTERACTIVE single-width train */
                    /* [0x31][idx:4] -> forever output pulses of HIGH width = idx cycles
                     * (idx*5 ns), low gap ~500 ns. The switch selects the forever-loop ONCE
                     * (before the loop) so dispatch never enters the timed high phase -> the
                     * HIGH width is the exact compile-time __delay_cycles, 5 ns per idx step.
                     * Zoom the scope on one pulse; sweeping idx walks the falling edge by 5 ns. */
                    uint32_t idx;
                    REQUIRE_LEN(5);
                    idx = rd32(&payload[1]);
                    switch (idx) {
                    case 1:  for(;;){ __R30|=OUT0;                     __R30&=~OUT0; __delay_cycles(10);}
                    case 2:  for(;;){ __R30|=OUT0; __delay_cycles(1);  __R30&=~OUT0; __delay_cycles(10);}
                    case 3:  for(;;){ __R30|=OUT0; __delay_cycles(2);  __R30&=~OUT0; __delay_cycles(10);}
                    case 4:  for(;;){ __R30|=OUT0; __delay_cycles(3);  __R30&=~OUT0; __delay_cycles(10);}
                    case 5:  for(;;){ __R30|=OUT0; __delay_cycles(4);  __R30&=~OUT0; __delay_cycles(10);}
                    case 6:  for(;;){ __R30|=OUT0; __delay_cycles(5);  __R30&=~OUT0; __delay_cycles(10);}
                    case 7:  for(;;){ __R30|=OUT0; __delay_cycles(6);  __R30&=~OUT0; __delay_cycles(10);}
                    case 8:  for(;;){ __R30|=OUT0; __delay_cycles(7);  __R30&=~OUT0; __delay_cycles(10);}
                    case 9:  for(;;){ __R30|=OUT0; __delay_cycles(8);  __R30&=~OUT0; __delay_cycles(10);}
                    case 10: for(;;){ __R30|=OUT0; __delay_cycles(9);  __R30&=~OUT0; __delay_cycles(10);}
                    case 11: for(;;){ __R30|=OUT0; __delay_cycles(10); __R30&=~OUT0; __delay_cycles(10);}
                    case 12: for(;;){ __R30|=OUT0; __delay_cycles(11); __R30&=~OUT0; __delay_cycles(10);}
                    case 16: for(;;){ __R30|=OUT0; __delay_cycles(15); __R30&=~OUT0; __delay_cycles(10);}
                    case 20: for(;;){ __R30|=OUT0; __delay_cycles(19); __R30&=~OUT0; __delay_cycles(10);}
                    case 40: for(;;){ __R30|=OUT0; __delay_cycles(39); __R30&=~OUT0; __delay_cycles(10);}
                    default: for(;;){ __R30|=OUT0; __delay_cycles(9);  __R30&=~OUT0; __delay_cycles(10);}
                    }
                    break;
                }

                case 0x32:                                  /* AUTO 5 ns-step sweep (scope demo) */
                    /* Forever: hold each HIGH width (~0.7 s) then step it by exactly 1 cycle
                     * (5 ns), cycling 5..60 ns and repeating. The PRU does the stepping itself
                     * (no host restart, no output blanking). Zoom the scope on one pulse and
                     * trigger on its rising edge: the falling edge walks out by 5 ns each step. */
                    for (;;) {
                        for (i=0;i<8000000u;i++){ __R30|=OUT0;                 __R30&=~OUT0; __delay_cycles(10);} /*  5 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(1);  __R30&=~OUT0; __delay_cycles(10);} /* 10 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(2);  __R30&=~OUT0; __delay_cycles(10);} /* 15 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(3);  __R30&=~OUT0; __delay_cycles(10);} /* 20 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(4);  __R30&=~OUT0; __delay_cycles(10);} /* 25 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(5);  __R30&=~OUT0; __delay_cycles(10);} /* 30 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(6);  __R30&=~OUT0; __delay_cycles(10);} /* 35 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(7);  __R30&=~OUT0; __delay_cycles(10);} /* 40 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(8);  __R30&=~OUT0; __delay_cycles(10);} /* 45 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(9);  __R30&=~OUT0; __delay_cycles(10);} /* 50 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(10); __R30&=~OUT0; __delay_cycles(10);} /* 55 ns */
                        for (i=0;i<8000000u;i++){ __R30|=OUT0; __delay_cycles(11); __R30&=~OUT0; __delay_cycles(10);} /* 60 ns */
                    }
                    break;

                case 0x36:                                  /* 10 Hz, nominal 50% duty square wave on P8_11 */
                    /* Each phase has the same 10,000,000 NOP-cycle (50 ms) PRU delay,
                     * split into <= 16-bit LOOP blocks. The fixed call/edge overhead is
                     * symmetric for HIGH and LOW, so the duty ratio remains 50% to within
                     * a few deterministic PRU cycles.
                     * This is a continuous demo; stop it by reloading firmware. */
                    for (;;) {
                        __R30 |= OUT0;
                        for (i = 0; i < HALF_PERIOD_10HZ_FULL_BLOCKS; i++)
                            pru_delay(PRU_DELAY_MAX_CYCLES);
                        pru_delay(HALF_PERIOD_10HZ_REMAINDER);
                        __R30 &= ~OUT0;
                        for (i = 0; i < HALF_PERIOD_10HZ_FULL_BLOCKS; i++)
                            pru_delay(PRU_DELAY_MAX_CYCLES);
                        pru_delay(HALF_PERIOD_10HZ_REMAINDER);
                    }
                    break;

                case 0x43: {                                /* OUTPUT->INPUT WINDOW STROBE (R4, external) */
                    /* EXTERNAL proof the output<->input sampling WINDOW is positioned in 5 ns steps,
                     * vs the AWG crystal-traceable delay (NOT a self-referential loopback).
                     * out0(P8_11)->AWG ext-trig ; AWG echo->in(P8_16).
                     * [43][D:4] -> fire output, then pru_delay(D) (pru_delay.asm HW LOOP: EXACTLY D
                     * cycles = D*5 ns, zero per-iteration overhead -> no eCAP-spin ~6-cycle wobble)
                     * -> sample the AWG echo ONCE. Host
                     * sweeps D to find the 0->1 transition D* (the echo edge); stepping AWG TRIG:DEL
                     * tau moves D* by tau/5ns ticks => window tracks the external reference at 5 ns.
                     * Reply [43][ok][bit][D:4] (7B). */
                    uint32_t D, bit; volatile uint32_t z;
                    REQUIRE_LEN(5);
                    D = rd32(&payload[1]);                        /* window offset in PRU cycles (5ns) */
                    if (D > 4000u) D = 4000u;
                    __R30 &= ~OUT0; __delay_cycles(2000);         /* settle + AWG re-arm */
                    for (z = 0; (__R31 & IN0); z++) { if (z >= 60000u) break; }  /* ensure input low */
                    __R30 |= OUT0;                                /* fire output -> AWG trig */
                    pru_delay(D);                                 /* EXACT D-cycle (D*5ns) HW-LOOP, no wobble */
                    bit = (__R31 & IN0) ? 1u : 0u;               /* sample AWG echo at out + D*5ns (exact) */
                    __R30 &= ~OUT0;
                    payload[0]=0x43; payload[1]=1; payload[2]=(uint8_t)bit; wr32(&payload[3], D);
                    reply(7);
                    break;
                }

                case 0x50:                                  /* RTASE BUFFER RESET */
                    rtase_symbol_count = 0;
                    payload[0]=0x50; payload[1]=1; wr16(&payload[2], 0);
                    reply(4);
                    break;

                case 0x51: {                                /* RTASE APPEND PACKED 2-BIT VALUES */
                    /* [51][symbol_count:2][four symbols per following byte,
                     * least-significant pair first].  Chunks may end at any
                     * symbol boundary; the PRU repacks them into one buffer. */
                    uint16_t n, packed_bytes, j;
                    REQUIRE_LEN(4);
                    n = rd16(&payload[1]);
                    packed_bytes = (uint16_t)((n + 3u) >> 2);
                    if (n == 0 || len != (uint16_t)(3u + packed_bytes) ||
                        (uint32_t)rtase_symbol_count + n > RTASE_MAX_SYMBOLS) {
                        payload[0]=0x51; payload[1]=0xEF; reply(2); break;
                    }
                    for (j = 0; j < n; j++) {
                        uint8_t value =
                            (payload[3u + (j >> 2)] >> ((j & 3u) << 1)) & 3u;
                        rtase_set_symbol((uint16_t)(rtase_symbol_count + j), value);
                    }
                    rtase_symbol_count = (uint16_t)(rtase_symbol_count + n);
                    payload[0]=0x51; payload[1]=1;
                    wr16(&payload[2], rtase_symbol_count); reply(4);
                    break;
                }

                case 0x52: {                                /* RTASE FINITE RUN */
                    /* [52][settle_cycles:2][pulse_cycles:2][gap_cycles:4]
                     *     [shots:4]
                     * For each shot: drive SEL, wait, pulse UPDATE/D1CLK, wait
                     * the non-critical display/re-arm gap.  A finite command is
                     * intentional: remoteproc remains recoverable without a
                     * separate asynchronous stop channel. */
                    uint16_t settle_cycles, pulse_cycles;
                    uint32_t gap_cycles, shots;
                    uint8_t selected = 0;
                    REQUIRE_LEN(13);
                    settle_cycles = rd16(&payload[1]);
                    pulse_cycles = rd16(&payload[3]);
                    gap_cycles = rd32(&payload[5]);
                    shots = rd32(&payload[9]);
                    if (rtase_symbol_count == 0 || pulse_cycles == 0 ||
                        shots == 0 || shots > 100000u) {
                        payload[0]=0x52; payload[1]=0xEF; reply(2); break;
                    }
                    __R30 &= ~RTASE_OUTPUTS;
                    for (i = 0; i < shots; i++) {
                        selected = rtase_symbol((uint16_t)(i % rtase_symbol_count));
                        __R30 = (__R30 & ~RTASE_OUTPUTS) |
                                ((selected & 1u) ? OUT0 : 0u) |
                                ((selected & 2u) ? OUT1 : 0u);
                        pru_delay(settle_cycles);
                        __R30 |= RTASE_UPDATE;
                        pru_delay(pulse_cycles);
                        __R30 &= ~RTASE_UPDATE;
                        rtase_delay_long(gap_cycles);
                    }
                    __R30 &= ~RTASE_OUTPUTS;
                    payload[0]=0x52; payload[1]=1; wr32(&payload[2], shots);
                    payload[6]=selected; reply(7);
                    break;
                }

                case 0x53:                                  /* RTASE STATUS */
                    payload[0]=0x53; payload[1]=1;
                    wr16(&payload[2], rtase_symbol_count);
                    payload[4]=(uint8_t)((__R30 & RTASE_OUTPUTS) ? 1u : 0u);
                    reply(5);
                    break;

                case 0x34:                                  /* OUT0->OUT1 DELAY RAMP 0..100ns (R4 demo) */
                    /* Two channels: out0=P8_11 (R30.15), out1=P8_12 (R30.14). Each frame raises
                     * out1 a programmed delay AFTER out0; the delay steps 0,5,10,...,100 ns and
                     * loops. 2-ch scope: ch1=P8_11, ch2=P8_12, trigger ch1 rising + PERSISTENCE
                     * -> out1's edge fans out from 0 ns in exact 5 ns steps. k=0 = both edges in
                     * ONE instruction (true 0 ns); k>=1 = out1 delayed k cycles (k*5 ns). */
                    for (;;) {
                        __R30|=(OUT0|OUT1);                __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /*  0 ns */
                        __R30|=OUT0;                       __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /*  5 ns */
                        __R30|=OUT0; __delay_cycles(1);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 10 ns */
                        __R30|=OUT0; __delay_cycles(2);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 15 ns */
                        __R30|=OUT0; __delay_cycles(3);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 20 ns */
                        __R30|=OUT0; __delay_cycles(4);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 25 ns */
                        __R30|=OUT0; __delay_cycles(5);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 30 ns */
                        __R30|=OUT0; __delay_cycles(6);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 35 ns */
                        __R30|=OUT0; __delay_cycles(7);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 40 ns */
                        __R30|=OUT0; __delay_cycles(8);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 45 ns */
                        __R30|=OUT0; __delay_cycles(9);    __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 50 ns */
                        __R30|=OUT0; __delay_cycles(11);   __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 60 ns */
                        __R30|=OUT0; __delay_cycles(13);   __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 70 ns */
                        __R30|=OUT0; __delay_cycles(15);   __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 80 ns */
                        __R30|=OUT0; __delay_cycles(17);   __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 90 ns */
                        __R30|=OUT0; __delay_cycles(19);   __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(60); /* 100 ns */
                    }
                    break;

                case 0x35: {                                /* SETTABLE out0->out1 delay [k:4] = k*5 ns */
                    /* Fixed delay = k cycles (k*5 ns) between out0 and out1 rising edges, forever,
                     * so you can park on one value and measure it on a 2-ch scope. k=0 => 0 ns
                     * (both edges in one instruction). Sweep k=0,1,2,... to verify 5 ns steps. */
                    uint32_t k;
                    REQUIRE_LEN(5);
                    k = rd32(&payload[1]);
                    switch (k) {
                    case 0:  for(;;){ __R30|=(OUT0|OUT1);              __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 1:  for(;;){ __R30|=OUT0;                     __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 2:  for(;;){ __R30|=OUT0; __delay_cycles(1);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 3:  for(;;){ __R30|=OUT0; __delay_cycles(2);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 4:  for(;;){ __R30|=OUT0; __delay_cycles(3);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 5:  for(;;){ __R30|=OUT0; __delay_cycles(4);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 6:  for(;;){ __R30|=OUT0; __delay_cycles(5);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 8:  for(;;){ __R30|=OUT0; __delay_cycles(7);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 10: for(;;){ __R30|=OUT0; __delay_cycles(9);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    case 20: for(;;){ __R30|=OUT0; __delay_cycles(19); __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);}
                    default: for(;;){ __R30|=OUT0; __delay_cycles(9);  __R30|=OUT1; __delay_cycles(40); __R30&=~(OUT0|OUT1); __delay_cycles(100);} /* unlisted k -> 50ns */
                    }
                    break;
                }

                default:
                    payload[0] = 0xEE;
                    payload[1] = 0xEE;
                    reply(2);
                    break;
                }
            }
        }
    }
}

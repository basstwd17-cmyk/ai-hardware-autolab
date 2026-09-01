// Fast bring-up path: prove BBB -> CPLD propagation without touching either
// DAC bus, DAC clock, SPI clock, or the analog output path.
//
// BBB PRU0 R30.15 drives P8_11. RTASE_V1 routes P8_11 as SYM.A0 to CPLD
// pin 85 / PR13B. The only driven CPLD output is the already-probed isolated
// pin 117.

module bbb_cpld_loopback (
    input  wire bbb_p8_11,
    output wire dbg0
);

    assign dbg0 = bbb_p8_11;

endmodule

// RTASE_V1 first end-to-end DAC demo (channel A / RF1 only).
//
// BBB PRU0 presents a two-bit selection on SYM.A[1:0].  After keeping that
// selection stable for the requested delay it pulses LDAC.A.  The CPLD maps
// the selection to one of four fixed 14-bit codes and forwards the pulse to
// the AD9744 CLOCK input.  DAC2/RF2 is deliberately absent from this module,
// so its bus and clock remain unused CPLD inputs.

module rtase_dac_demo (
    input  wire        sym_a0,
    input  wire        sym_a1,
    input  wire        ldac_a,
    output reg  [13:0] d1b,
    output wire        d1clk
);
    // Four evenly spaced proof-of-path levels.  These are raw DAC codes, not
    // calibrated RF1 voltages.  Calibration can later replace these constants
    // with writable CODE0..CODE3 registers without changing the timing path.
    localparam [13:0] CODE0 = 14'h0000;
    localparam [13:0] CODE1 = 14'h1555;
    localparam [13:0] CODE2 = 14'h2AAA;
    localparam [13:0] CODE3 = 14'h3FFF;

    always @* begin
        case ({sym_a1, sym_a0})
            2'b00: d1b = CODE0;
            2'b01: d1b = CODE1;
            2'b10: d1b = CODE2;
            default: d1b = CODE3;
        endcase
    end

    // The PRU creates the update timing.  Keeping this path combinational
    // avoids a second, unrelated CPLD clock domain in the first demonstration.
    assign d1clk = ldac_a;
endmodule

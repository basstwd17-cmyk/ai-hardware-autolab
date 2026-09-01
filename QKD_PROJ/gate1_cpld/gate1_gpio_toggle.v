// Gate 1 bring-up design for RTASE_V1 (LCMXO2-4000HC-4TG144C).
//
// Purpose: drive exactly one electrically isolated pin with a slow square wave
// so that JTAG programming and a scope measurement can be proven, without
// touching anything else on the board.
//
// This is deliberately NOT derived from Lattice/bbb_cpld_test.v. That design
// takes sel0/sel1 as inputs on CPLD pins 35 and 34, which the schematic shows
// as LDASC.A and SPI.CLK -- board nets, not spare debug pins. Reusing it would
// carry that problem into Gate 1. Keep the two projects separate.
//
// Ports: dbg0 only. No DAC, no SPI, no BBB select lines. If a port is ever
// added here, gate1-test-spec.yaml and the pad report check must be updated
// together, because the whole safety argument is "one pin, nothing else".

module gate1_gpio_toggle (
    output wire dbg0
);

    // MachXO2 internal oscillator. NOM_FREQ is restricted to a fixed list by
    // the family; 2.08 MHz is the lowest and the default, which keeps the
    // divider small.
    //
    // Its accuracy is NOT specified in the Diamond libraries or the local
    // documentation. Do not derive a frequency pass criterion from the nominal
    // value -- see gate1-test-spec.yaml, osch_accuracy. Gate 1 only needs to
    // show that a safe output toggles repeatably, not that it is precisely 1 Hz.
    localparam OSC_NOM_FREQ = "2.08";

    // 2.08 MHz / (2 * 1040000) = 1 Hz nominal.
    localparam HALF_PERIOD_CYCLES = 1040000;
    localparam COUNTER_WIDTH      = 20;  // 2^20 = 1048576 > 1040000

    wire osc_clk;

    OSCH #(
        .NOM_FREQ (OSC_NOM_FREQ)
    ) internal_osc (
        .STDBY    (1'b0),
        .OSC      (osc_clk),
        .SEDSTDBY ()
    );

    // No external reset exists on this board, so the registers rely on the
    // MachXO2 power-up state. The initial values below are what LSE maps onto
    // it; without them the counter would start undefined in simulation.
    reg [COUNTER_WIDTH-1:0] divider = {COUNTER_WIDTH{1'b0}};
    reg                     level   = 1'b0;

    always @(posedge osc_clk) begin
        if (divider == (HALF_PERIOD_CYCLES - 1)) begin
            divider <= {COUNTER_WIDTH{1'b0}};
            level   <= ~level;
        end else begin
            divider <= divider + 1'b1;
        end
    end

    assign dbg0 = level;

endmodule

module bbb_cpld_test (
    input  wire sel0,     // BBB P8_45 ¡æ CPLD pin 35
    input  wire sel1,     // BBB P8_46 ¡æ CPLD pin 34

    output wire dbg0,     // CPLD pin 117
    output wire dbg1,     // CPLD pin 121
    output wire dbg_xor   // CPLD pin 122
);

assign dbg0    = sel0;
assign dbg1    = sel1;
assign dbg_xor = sel0 ^ sel1;

endmodule
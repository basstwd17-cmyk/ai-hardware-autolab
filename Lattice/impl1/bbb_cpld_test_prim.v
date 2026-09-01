// Verilog netlist produced by program LSE :  version Diamond (64-bit) 3.14.0.75.2
// Netlist written on Thu Aug 13 10:19:33 2026
//
// Verilog Description of module bbb_cpld_test
//

module bbb_cpld_test (sel0, sel1, dbg0, dbg1, dbg_xor) /* synthesis syn_module_defined=1 */ ;   // c:/kist/lattice/bbb_cpld_test.v(1[8:21])
    input sel0;   // c:/kist/lattice/bbb_cpld_test.v(2[17:21])
    input sel1;   // c:/kist/lattice/bbb_cpld_test.v(3[17:21])
    output dbg0;   // c:/kist/lattice/bbb_cpld_test.v(5[17:21])
    output dbg1;   // c:/kist/lattice/bbb_cpld_test.v(6[17:21])
    output dbg_xor;   // c:/kist/lattice/bbb_cpld_test.v(7[17:24])
    
    
    wire dbg0_c_c, dbg1_c_c, dbg_xor_c, GND_net, VCC_net;
    
    TSALL TSALL_INST (.TSALL(GND_net));
    VLO i24 (.Z(GND_net));
    OB dbg0_pad (.I(dbg0_c_c), .O(dbg0));   // c:/kist/lattice/bbb_cpld_test.v(5[17:21])
    OB dbg1_pad (.I(dbg1_c_c), .O(dbg1));   // c:/kist/lattice/bbb_cpld_test.v(6[17:21])
    OB dbg_xor_pad (.I(dbg_xor_c), .O(dbg_xor));   // c:/kist/lattice/bbb_cpld_test.v(7[17:24])
    IB dbg0_c_pad (.I(sel0), .O(dbg0_c_c));   // c:/kist/lattice/bbb_cpld_test.v(2[17:21])
    IB dbg1_c_pad (.I(sel1), .O(dbg1_c_c));   // c:/kist/lattice/bbb_cpld_test.v(3[17:21])
    GSR GSR_INST (.GSR(VCC_net));
    PUR PUR_INST (.PUR(VCC_net));
    defparam PUR_INST.RST_PULSE = 1;
    LUT4 sel0_I_0_2_lut (.A(dbg0_c_c), .B(dbg1_c_c), .Z(dbg_xor_c)) /* synthesis lut_function=(!(A (B)+!A !(B))) */ ;   // c:/kist/lattice/bbb_cpld_test.v(12[18:29])
    defparam sel0_I_0_2_lut.init = 16'h6666;
    VHI i25 (.Z(VCC_net));
    
endmodule
//
// Verilog Description of module TSALL
// module not written out since it is a black-box. 
//

//
// Verilog Description of module PUR
// module not written out since it is a black-box. 
//


// Isolate only the standard CPLD-to-BBB return path.  U2 pin 33 is held at
// LVCMOS33 logic-high with a 4 mA slow output.  All other user I/O is unused.
module spi_standard_return_static_high (
    output wire spi_to_bbb_d0
);
    assign spi_to_bbb_d0 = 1'b1;
endmodule

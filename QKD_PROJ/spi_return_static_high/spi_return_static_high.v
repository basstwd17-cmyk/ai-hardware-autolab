// Isolate only the CPLD-to-BBB reverse return path.  U2 pin 31 is held at
// LVCMOS33 logic-high with a 4 mA slow output.  All other user I/O is unused.
module spi_return_static_high (
    output wire spi_to_bbb_d1
);
    assign spi_to_bbb_d1 = 1'b1;
endmodule

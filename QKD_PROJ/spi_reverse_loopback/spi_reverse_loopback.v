// Safe SPI direction test B: BBB SPI0 D0/D1 direction is reversed together
// with the CPLD pads.  The BBB drives P9_21 / U2 pin 33, and U2 pin 31
// returns that level to BBB P9_18 while CS is low.  No DAC-related output is
// present in this image.

module spi_reverse_loopback (
    input  wire spi_from_bbb_d0,
    input  wire spi_cs_n,
    output wire spi_to_bbb_d1
);
    assign spi_to_bbb_d1 = spi_cs_n ? 1'b0 : spi_from_bbb_d0;
endmodule

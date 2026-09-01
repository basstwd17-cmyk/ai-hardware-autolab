// Isolate the reverse SPI0 data path only.  U2 pin 33 receives BBB P9_21
// (SPI0_D0) and U2 pin 31 returns it to BBB P9_18 (SPI0_D1).  CS and SCLK
// are deliberately absent, and no DAC/SMA/PRU-related pin is an output.
module spi_reverse_ungated_loopback (
    input  wire spi_from_bbb_d0,
    output wire spi_to_bbb_d1
);
    assign spi_to_bbb_d1 = spi_from_bbb_d0;
endmodule

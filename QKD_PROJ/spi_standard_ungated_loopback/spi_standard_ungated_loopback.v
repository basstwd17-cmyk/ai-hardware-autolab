// Isolate the production-standard SPI0 data path only.  U2 pin 31 receives
// BBB P9_18 (SPI0_D1/MOSI) and U2 pin 33 returns it to BBB P9_21
// (SPI0_D0/MISO). CS/SCLK and all DAC/SMA/PRU pins are absent.
module spi_standard_ungated_loopback (
    input  wire spi_from_bbb_d1,
    output wire spi_to_bbb_d0
);
    assign spi_to_bbb_d0 = spi_from_bbb_d1;
endmodule

// Safe SPI direction test A: BBB standard SPI0 direction only.
//
// BBB P9_18 / SPI0_D1 is the source and reaches U2 pin 31.
// U2 pin 33 returns that exact level to BBB P9_21 / SPI0_D0 while CS is low.
// No DAC data, DAC clock, PRU, or analog-output pin is present in this design.

module spi_standard_loopback (
    input  wire spi_mosi,
    input  wire spi_cs_n,
    output wire spi_miso
);
    assign spi_miso = spi_cs_n ? 1'b0 : spi_mosi;
endmodule

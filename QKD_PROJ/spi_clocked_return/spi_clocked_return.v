// Safe mode-0 SPI receive-timing diagnostic.
//
// No DAC, SMA, PRU, data-clock, or status output exists in this design.  It
// returns a fixed six-byte response only through U2 pin 33 (SPI.MISO).  The
// first bit is loaded while CS is idle/high; every subsequent bit advances on
// the SCLK falling edge.  A mode-0 master therefore has a half-cycle of setup
// time before each rising-edge sample.
module spi_clocked_return (
    input  wire spi_sclk,
    input  wire spi_cs_n,
    output wire spi_miso
);
    localparam [47:0] RESPONSE = 48'hA55A3CC3F00F;
    reg [47:0] response_shift;

    initial response_shift = RESPONSE;

    // CS rising ends a frame and preloads the response for the next frame.
    // While CS is low, every falling edge moves the next bit into place.
    always @(posedge spi_cs_n or negedge spi_sclk) begin
        if (spi_cs_n)
            response_shift <= RESPONSE;
        else
            response_shift <= {response_shift[46:0], 1'b0};
    end

    assign spi_miso = spi_cs_n ? 1'b0 : response_shift[47];
endmodule
